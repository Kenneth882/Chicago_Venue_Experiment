# SPEC.md — Build Phase 1: Scaffold + Milestones 1–3

This spec defines the first build phase of the sourcing agent. Read CLAUDE.md first — it
holds the architecture, invariants, and conventions. This document says exactly what to
build now, in what order, and how we'll know it works.

**In scope:** repo scaffold, database layer, Places API client, Stage 1 (search workers),
Stage 2 (cheap filter gate).
**Out of scope for this phase:** Stage 3 extraction, Stage 4 scoring, Playwright, Supabase,
Batch API. Create `src/stage3_extract.py` and `src/stage4_score.py` as stubs with
docstrings only.

---

## Work order 0 — Scaffold

Create the repo layout from CLAUDE.md. Specifics:

- `pyproject.toml` or `requirements.txt` with: httpx, anthropic, shapely, pdf2image,
  Pillow, beautifulsoup4, tenacity, python-dotenv, pytest.
- `.env.example` with `GOOGLE_PLACES_API_KEY=` and `ANTHROPIC_API_KEY=`.
- `.gitignore`: `.env`, `tracker.db`, `cache/`, `__pycache__/`, `.venv/`.
- `README.md`: 10 lines max — setup commands and how to run each milestone check.

### config/zones.json (create with exactly this content, radii in meters)

```json
{
  "zones": [
    {"zone_id": "river_north",  "name": "River North",             "tier": 1, "center": {"lat": 41.8924, "lng": -87.6341}, "radius_m": 1000},
    {"zone_id": "west_loop",    "name": "West Loop / Fulton Market","tier": 1, "center": {"lat": 41.8846, "lng": -87.6480}, "radius_m": 1200},
    {"zone_id": "loop",         "name": "The Loop",                "tier": 1, "center": {"lat": 41.8786, "lng": -87.6297}, "radius_m": 1000},
    {"zone_id": "streeterville","name": "Streeterville",           "tier": 2, "center": {"lat": 41.8925, "lng": -87.6196}, "radius_m": 800},
    {"zone_id": "gold_coast",   "name": "Gold Coast",              "tier": 2, "center": {"lat": 41.9048, "lng": -87.6297}, "radius_m": 800},
    {"zone_id": "old_town",     "name": "Old Town",                "tier": 2, "center": {"lat": 41.9110, "lng": -87.6376}, "radius_m": 800},
    {"zone_id": "lincoln_park", "name": "Lincoln Park",            "tier": 2, "center": {"lat": 41.9214, "lng": -87.6513}, "radius_m": 1200},
    {"zone_id": "wicker_park",  "name": "Wicker Park / Bucktown",  "tier": 2, "center": {"lat": 41.9088, "lng": -87.6773}, "radius_m": 1100},
    {"zone_id": "logan_square", "name": "Logan Square",            "tier": 3, "center": {"lat": 41.9234, "lng": -87.7078}, "radius_m": 1100},
    {"zone_id": "south_loop",   "name": "South Loop",              "tier": 3, "center": {"lat": 41.8676, "lng": -87.6270}, "radius_m": 1000}
  ]
}
```

(Coordinates are neighborhood centers; a human will sanity-check them on a map before M6.
Treat them as config, not code.)

### config/venue_types.json (create with exactly this content)

```json
{
  "venue_types": [
    {"type_id": "cocktail_bar",    "included_type": "bar",
     "queries": ["cocktail bar {zone} Chicago", "craft cocktails {zone}", "cocktail lounge private events {zone}"]},
    {"type_id": "private_dining",  "included_type": "restaurant",
     "queries": ["restaurant private dining {zone} Chicago", "private dining room {zone}", "group dining events {zone} Chicago"]},
    {"type_id": "event_venue",     "included_type": null,
     "queries": ["private event venue {zone} Chicago", "event space {zone} Chicago"]},
    {"type_id": "brewery",         "included_type": "bar",
     "queries": ["brewery taproom {zone} Chicago", "brewery private events {zone}"]},
    {"type_id": "rooftop",         "included_type": "bar",
     "queries": ["rooftop bar {zone} Chicago", "rooftop private events {zone} Chicago"]}
  ]
}
```

`{zone}` is substituted with the zone display name at runtime. A cell = one zone × one
venue type; its `cell_id` = `{zone_id}__{type_id}`. 10 zones × 5 types = 50 cells.

---

## Work order 1 — src/db.py + schema

SQLite via stdlib `sqlite3`, WAL mode, one connection factory. Postgres-compatible DDL.
No other module may import sqlite3.

```sql
CREATE TABLE IF NOT EXISTS venues (
  place_id        TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  formatted_address TEXT,
  lat REAL, lng REAL,
  zone_id         TEXT,
  rating          REAL,
  user_rating_count INTEGER,
  price_level     INTEGER,
  business_status TEXT,
  website_uri     TEXT,
  types_json      TEXT,          -- JSON array
  primary_type    TEXT,
  stage           TEXT NOT NULL DEFAULT '0_raw',
                  -- 0_raw | 1_geo_ok | 2_filtered_ok | eliminated | needs_review
                  -- (later: 3_enriched, 0_sourced)
  eliminated_reason TEXT,
  found_by_json   TEXT NOT NULL, -- JSON array of {cell_id, query, rank, run_id}
  extraction_json TEXT,          -- Stage 3 fills this (null for now)
  score           REAL,          -- Stage 4 fills this (null for now)
  first_seen_at   TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cells (
  cell_id   TEXT PRIMARY KEY,
  zone_id   TEXT NOT NULL,
  type_id   TEXT NOT NULL,
  status    TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
  run_id    TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS run_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, cell_id TEXT NOT NULL,
  raw_results INTEGER, killed_geo INTEGER, killed_type INTEGER,
  new_rows INTEGER, dupes INTEGER, hit_60_cap INTEGER,  -- 0/1
  created_at TEXT NOT NULL
);
```

Public functions (exact signatures up to you, behavior fixed):
`init_db()`, `seed_cells(zones, venue_types)`, `claim_pending_cell(run_id)`,
`complete_cell(cell_id, status)`, `upsert_venue(record) -> "new"|"dupe"`,
`append_found_by(place_id, entry)`, `set_stage(place_id, stage, reason=None)`,
`write_stats(row)`.

**Upsert semantics:** on conflict of place_id, do NOT overwrite scalar fields with nulls;
append to `found_by_json`; bump `updated_at`; return `"dupe"`.

---

## Work order 2 — src/places.py (Milestone 1)

Client for Places API (New) `POST https://places.googleapis.com/v1/places:searchText`.

- Headers: `X-Goog-Api-Key`, `X-Goog-FieldMask` with exactly the mask from CLAUDE.md
  (plus `nextPageToken`).
- Body: `textQuery`, `locationRestriction: {rectangle: {low, high}}` — the bounding box
  of the cell's circle — optional `includedType`, `pageToken` when paginating.
  (Amended 2026-07-07: the spec originally said `circle`, but Text Search rejects
  `circle` inside `locationRestriction` with 400 INVALID_ARGUMENT — circle-restriction
  exists only on Nearby Search, and `locationBias` circle is soft. The hard rectangle
  restriction + Stage 1's shapely point-in-circle check preserves the intended circular
  boundary, enforced in code. See NOTES.md for the exact error.)
- `search_text(query, center, radius_m, included_type=None) -> list[dict]`: paginates up
  to 3 pages / 60 results, hard page cap enforced in code.
- tenacity: exponential backoff on 429/5xx (max 5 tries). On INVALID_REQUEST with a fresh
  pageToken: sleep 2s, retry once.
- `--dry-run` support at the caller level: log the request that would be sent, return [].

**M1 acceptance:** `python -m src.stage1_search --cell river_north__cocktail_bar --limit 1`
prints a table of results (name, rating, review count, website, place_id) for one query,
paginating to page 3 when tokens are returned. Runs with a real key; no DB writes needed
for this check (use `--no-write`).

---

## Work order 3 — src/stage1_search.py (Milestone 2)

CLI: `python -m src.stage1_search [--cell CELL_ID | --zone ZONE_ID | --all]
[--dry-run] [--limit N] [--no-write]`

Per cell: for each query (with `{zone}` substituted), call `search_text`. Per result:

1. Point-in-polygon: shapely distance from zone center ≤ radius_m (circle check). Fail →
   counted `killed_geo`, not written.
2. Type blocklist: if `types` contains `lodging`, `casino`, or `liquor_store`, or
   `primary_type == "night_club"` with no bar/restaurant type present → `killed_type`,
   not written.
   (Amended 2026-07-08: the blocklist now lives in hand-reviewed
   `config/blocked_types.json` — `blocked_types` match anywhere in `types` (unchanged
   trio above); `blocked_primary_types` match `primary_type` ONLY and unconditionally:
   nightclubs (the bar/restaurant-hybrid exemption is gone), coffee/breakfast spots
   (cafe, coffee_shop, breakfast_restaurant), and activity venues (barber_shop,
   miniature_golf_course, movie_theater, sports_complex, bowling_alley,
   amusement_center, karaoke). Rationale: corporate-networking fit — these categories
   sell the wrong event model. Primary-only matching is deliberate: good venues carry
   brunch_restaurant/cafe as secondary tags. Stage 2 mirrors the check as its first
   (cheapest) offline gate with reason code `blocked_type`, and
   `python -m src.stage2_filter --sweep-blocked-types` retroactively eliminates live
   rows after any config edit — idempotent, never hard-deletes, so misclassified
   venues (e.g. Celeste, Blind Barber) can be hand-resurrected.)
3. Normalize to the venues schema; `found_by` entry = {cell_id, query, rank, run_id}.
4. `upsert_venue`; count new vs dupe.

**60-cap subdivision:** if any single query returns 60 results, split the cell's circle
into 4 sub-circles (radius/2, centers offset ±radius/2 in lat and lng directions) and
re-run that query against each. One recursion level only. Set `hit_60_cap = 1` in stats.

Write one run_stats row per cell. Mark cell done. Stamp everything with
`run_id = YYYYMMDD-HHMMSS` generated at process start.

**M2 acceptance:**
1. `python -m src.stage1_search --cell west_loop__cocktail_bar --cell river_north__cocktail_bar`
   completes and prints the stats table.
2. Running the same command again: `new_rows == 0`, dupes > 0, zero duplicate place_ids
   in the venues table (`SELECT place_id, COUNT(*) ... HAVING COUNT(*) > 1` returns empty).
3. `--dry-run` makes zero HTTP calls (assert via logged request count).

---

## Work order 4 — src/stage2_filter.py (Milestone 3)

CLI: `python -m src.stage2_filter [--limit N] [--dry-run]` — operates on all venues at
stage `1_geo_ok` (Stage 1 sets survivors to `1_geo_ok`).

Checks in this exact order, stopping at first failure; each failure sets
`stage='eliminated'` + reason code:

(Amended 2026-07-08: the rating/review-count gate — originally check 2,
`rating >= 4.0 AND user_rating_count >= 50` → `rating_below_4` /
`too_few_reviews` — is removed entirely and the remaining checks are
renumbered. Quality is scored, not gated: Stage 4's rating*log(review_count)
weight (~20%, unchanged) handles it, so a low-rated venue ranks lower
instead of dying here. Both reason codes are no longer producible; the 43
rows previously killed by the gate were resurrected to `1_geo_ok` and
re-filtered through the remaining checks. This also moots the old
CLAUDE.md-25-vs-SPEC-50 review-floor discrepancy.)

1. Type blocklist (`config/blocked_types.json`, same rule as Stage 1 — added here
   2026-07-08 as the cheapest gate so config edits catch already-ingested rows)
   → else `blocked_type`
2. `business_status == "OPERATIONAL"` → else `not_operational`
3. `price_level is null OR price_level <= 3` → else `price_level_high`
4. `website_uri` present → else `no_website`
5. HTTP check (httpx, follow redirects, 10s timeout, browser-like header set,
   one retry after 3s on 403/429/timeout, via cached fetch layer in
   `src/fetch.py`): status 200 → pass on to check 6. Otherwise two-strike:
   the first failing run sets `stage='needs_review'` with marker
   `website_dead_once` (re-checked automatically on the next run); a second
   consecutive failing run sets `eliminated` / `website_dead`. Bot-challenge
   responses (403/429) are never written to the fetch cache.
   (Amended 2026-07-07: 12 false `website_dead` kills — restaurant hosting
   platforms (Popmenu, Owner.com) behind Cloudflare 403 the Python TLS stack
   regardless of headers; curl with identical headers gets 200. A real-browser
   fetch for these lands with Playwright in M4; until then blocked sites park
   in needs_review instead of dying.)
   (Amended 2026-07-08: before striking, a failed httpx fetch escalates ONCE
   to the hardened Playwright fetch built in M4 (`fetch.get_rendered`) — but
   only for bot-wall/transient statuses: 403/429, timeout/transport failure,
   5xx. Never for robots_disallowed and never for plain 4xx like 404, which
   are genuinely dead. A rendered 200 passes on to check 6 using the rendered
   HTML and final URL; a rendered failure falls into the two-strike policy
   above. The two-strike policy stays even though a strike now means both
   paths failed — hardened Playwright itself flakes on some bot walls (see
   NOTES.md: order.toasttab.com rendered for one venue and 403'd for another
   the same day). This drains the 12-venue JA3 retry queue from the
   2026-07-07 incident.)
6. Identity match: compare venue name against final redirect domain, `<title>`, and
   `og:site_name` using a normalized token-overlap score.
   Score ≥ 0.6 → pass. Score ≤ 0.2 → `website_identity_mismatch`.
   Middle band (0.2–0.6) → set `stage='needs_review'` for now. Do NOT call Claude in this
   phase — the LLM tiebreak is a later work order.

Survivors → `stage='2_filtered_ok'`. Log every elimination at INFO:
`place_id | name | reason`.

`src/fetch.py`: `get(url) -> CachedResponse` — cache to `cache/{sha256(url)}.json`
(status, final_url, headers subset, text); per-domain min interval 1.0s; real UA string.

**M3 acceptance:**
1. Run against M2's rows; print a funnel summary (count in → count per reason → count out).
2. A pytest that runs the offline checks against hand-built fixture records (no network)
   and asserts the right reason codes. (Originally checks 1–4 / 6 records; the
   rating/review fixtures went away with the 2026-07-08 gate removal.)
3. Manual: we spot-check 10 eliminated rows together and every reason is correct.

---

## Testing rules for this phase

- pytest for all pure logic (geo check, blocklist, upsert semantics, filter ordering,
  identity scoring) using fixtures — never live APIs in tests.
- One `tests/fixtures/places_response.json` captured from a real M1 run, used to test
  pagination and normalization offline.

## Definition of done for Phase 1

All three acceptance checks pass, `pytest` is green, and a fresh clone + `.env` can go
from zero to the M3 funnel summary using only README commands. Then we write the Phase 2
spec (Stage 3 golden set + extraction).