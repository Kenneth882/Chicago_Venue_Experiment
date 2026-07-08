# CLAUDE.md — Chicago Venue Sourcing Agent

## What this project is

A one-shot, pre-launch batch pipeline that produces **500 validated Chicago venue candidates**
(cocktail bars, restaurants with private dining, event venues, breweries, rooftops) for outreach
to tech-company customers. It runs locally, writes to a SQLite tracker, and is re-runnable at
any time. It is NOT a service, NOT real-time, and has no UI.

Success metric: ≥500 tracker rows at `stage = 0_sourced`, with <5% later eliminated by humans.

## Core design principles (do not violate these)

1. **LLMs read, code decides.** Claude API calls are used ONLY for: (a) one-time query-variant
   generation, (b) nav-link classification fallback, (c) menu/site extraction into strict JSON,
   (d) final two-line venue summaries. All thresholds, filters, scoring, and pass/fail logic
   live in deterministic Python. Never ask an LLM to apply a numeric threshold.
2. **Everything is idempotent.** All venue writes are UPSERTs keyed on Google `place_id`.
   Running any stage twice must produce zero duplicates and no data loss. Cells/venues carry a
   `stage` status column; crashed runs resume by re-claiming non-done work.
3. **Every kill has a reason code.** Any row eliminated at any stage gets an
   `eliminated_reason` string (e.g. `not_operational`, `rating_below_4`, `website_dead`,
   `website_identity_mismatch`, `outside_zone`, `blocked_type`, `price_too_high`). Never
   silently drop a row. Rows are never hard-deleted.
4. **Cache all web fetches.** Raw HTTP responses are cached to `cache/` keyed by URL hash.
   Re-running extraction must hit cache, not the live web.
5. **Full provenance on every row**: which cell/query found it, at what rank, which run_id,
   source URLs for every extracted number, and an `evidence` sentence from extraction.

## Architecture (5 stages, run in order)

- **Stage 0 — config**: `config/zones.json` (~10 neighborhoods, each with center lat/lng +
  radius_m), `config/venue_types.json`, and frozen query variants (2–3 phrasings per
  zone×type cell, ~50 cells total). Config is hand-reviewed and committed; never generated
  at runtime.
- **Stage 1 — search workers**: For each cell, loop its query strings through Google
  **Places API (New)** Text Search (`places:searchText`) with `locationRestriction`
  (rectangle bounding the cell's circle — the API rejects circle here; see SPEC.md WO2)
  and a lean field mask: `places.id, places.displayName, places.formattedAddress,
  places.location, places.rating, places.userRatingCount, places.priceLevel,
  places.businessStatus, places.websiteUri, places.types, places.primaryType`.
  Paginate via `nextPageToken` up to 60 results per query. If a query hits the 60 cap, split
  the cell into 4 half-radius sub-circles and re-run (one recursion level max). Per result:
  point-in-polygon check (shapely), type blocklist (`lodging`, `casino`, `liquor_store`,
  pure `night_club`), normalize, upsert. Emit per-cell stats: raw_results, killed_geo,
  killed_type, new_rows, dupes, hit_60_cap.
- **Stage 2 — cheap filter gate**, ordered cheapest-first, on data already fetched:
  `business_status == OPERATIONAL` → `rating >= 4.0 AND user_ratings_total >= 25` →
  `price_level <= 3` → website returns 200 after redirects → website identity match (final
  redirect domain + <title> + og:site_name fuzzy-matched against venue name; ambiguous
  middle scores go to a small Claude call, not everything).
- **Stage 3 — menu deep-dive** (the only LLM-heavy stage, ~800–1000 survivors):
  fetch homepage (httpx, real UA, 10s timeout, per-domain rate limit, cached) → discover
  menu/private-event pages via sitemap.xml grep, then nav-link keyword match with Claude
  fallback; known paths (`/menu /menus /food /drinks /cocktails /private-events /events
  /parties /groups /book`) are a last resort, tried only when sitemap and nav links find
  nothing → route by content type: HTML→clean text (bs4), PDF→images
  (pdf2image), images→downscale to ~1500px (Pillow), JS-shell pages→Playwright fallback
  only when HTML body is empty, no menu found→Places Photos API fallback (customers
  photograph menus) → ONE Claude extraction call per venue against the strict JSON schema
  below → deterministic price rules.
- **Stage 4 — master scoring**: hard gates (operational, verified site, in-zone, price not
  flagged, event-capability evidence), then weighted score in code: zone tier ~25%, price
  fit ~20%, `rating * log(review_count)` ~20%, capacity fit ~15%, direct email over
  form-only ~15%, multi-query found_by bonus ~5%. Rank, take top 500 (email-contactable
  first, form_only backfills), write `stage = 0_sourced`, generate a stratified 50-row
  audit sample, export CSV.

## Stage 3 extraction JSON schema (exact)

```json
{
  "menu_matches_venue": true,
  "cocktail_price_min": 14, "cocktail_price_max": 19,
  "entree_price_min": null, "entree_price_max": null,
  "fnb_minimum_usd": 3000,
  "buyout_price_usd": null,
  "semi_private_available": true,
  "stated_capacity": 80,
  "event_contact_email": "events@venue.com",
  "contact_method": "email | form_only | phone_only | none",
  "event_keywords_found": ["private events", "semi-private"],
  "confidence": "high | medium | low",
  "evidence": "one sentence citing where each key number came from"
}
```

Extraction prompt rules: include venue name + address and require `menu_matches_venue`
verification; **null over guessing** — never estimate a price; require `evidence`.

Price rules (in code): minimum or buyout > $10,000 with no cheaper option →
`flag_price_too_high` (kill). High buyout WITH semi-private/lower minimum → keep, flag
`buyout_high_but_flexible`. Compute `price_signal` from fnb_minimum bucket, else median
cocktail price tier. `confidence: low` or `menu_matches_venue: false` → `needs_review`
queue, not pass/fail.

## Tech stack

- Python 3.11+, venv
- httpx, anthropic, shapely, pdf2image (+poppler), Pillow, beautifulsoup4, tenacity
- Playwright: install only when first needed (Milestone 4)
- SQLite via stdlib `sqlite3`. Schema written Postgres-compatible (text/numeric/JSON
  thinking, no SQLite-only types) — we migrate to Supabase between M5 and M6.
- ALL database access goes through `src/db.py` (`upsert_venue`, `claim_cell`,
  `write_stats`, ...). No other module touches the connection. This is what makes the
  Supabase swap a one-file change.

## Repo layout

```
config/           zones.json, venue_types.json, queries frozen per cell
src/db.py         all DB access (only module that touches sqlite)
src/stage1_search.py
src/stage2_filter.py
src/stage3_extract.py
src/stage4_score.py
src/places.py     Places API client (field mask, pagination, retries)
src/fetch.py      cached, rate-limited HTTP + Playwright fallback
src/llm.py        all Anthropic calls (models, schemas, prompts in one place)
tests/golden/     ~10 hand-verified venues + expected extraction JSON
cache/            raw fetch cache (gitignored)
tracker.db        (gitignored)
.env              GOOGLE_PLACES_API_KEY, ANTHROPIC_API_KEY (gitignored)
```

## Model + cost policy

- Text extraction: Haiku-class. Vision (PDF/picture menus, Maps photos) and low-confidence
  retries: Sonnet-class. Stage 3 runs use the synchronous Messages API by default. The
  Batch API is only worth revisiting if the queue grows large enough that estimated sync
  cost meaningfully exceeds batch cost (roughly an order of magnitude above current
  volume) — adopting it means restructuring the interleaved fetch/extract flow into
  separate collect-all → submit → poll → write-back phases.
- Every module that calls a paid API must support `--dry-run` (log what would be called)
  and `--limit N`.
- Never loop pagination without a hard page cap (3 pages/query). Never fetch the same URL
  twice past the cache.

## Build order (work these milestones in order; each has a pass condition)

1. **M1**: one Text Search call, one cell, print results. Pass: field mask + pagination to
   page 3 work.
2. **M2**: full Stage 1 for two cells with upsert/dedup/stats. Pass: running twice → zero
   duplicate rows.
3. **M3**: Stage 2 on those rows. Pass: 10 spot-checked kills all have correct reason codes.
4. **M4**: Stage 3 against `tests/golden/` (include: 1 HTML menu, 1 PDF menu, 1
   picture-only menu, 1 JS-heavy site, 1 no-menu venue). Pass: known prices match; every
   miss is null, never a hallucinated number. Golden set = regression test for any prompt
   change.
5. **M5**: Stage 4 scoring on the small set; eyeball ranking; tune weights.
6. **M6**: full 2-zone dry run → hand-audit 20 rows → full 10-zone run → stratified 50-row
   audit sample → CSV export.

Do not start a milestone until the previous one's pass condition is demonstrated.

## Conventions

- Type hints everywhere; dataclasses or pydantic models for Venue, Cell, ExtractionResult.
- Every run gets a `run_id` (timestamp) stamped on all writes.
- tenacity retry with exponential backoff on 429/5xx; treat Places INVALID_REQUEST on a
  fresh pageToken as retry-once-after-delay.
- Respect robots.txt; per-domain rate limit ≥1s between requests to the same host.
- No secrets in code or logs. Log elimination decisions at INFO with place_id + reason.

## Things to never do

- Never let an LLM apply the $10k rule, the 4.0 rating floor, or any threshold.
- Never generate search queries at runtime — they are frozen in config.
- Never hard-delete a venue row.
- Never call live APIs from tests — golden tests run against `cache/` fixtures.
- Never create duplicate rows for the same place_id.
- Never mark a venue dead for having no findable menu — that's `menu_unavailable`, a flag.