# NOTES.md — session lessons and quirks

Read this at the start of every session. One entry per lesson; update in place
rather than duplicating.

## Environment
- Local Python is 3.11.7 with SQLite 3.41.2 — `RETURNING` is available (needs
  ≥3.35), so `claim_pending_cell` is a single atomic UPDATE...RETURNING. If
  this ever runs on an older SQLite, that function needs a select-then-update
  fallback.
- venv at `.venv/`; all requirements installed cleanly on macOS (shapely and
  pdf2image wheels fine; poppler itself NOT yet installed — needed at M4, not
  before).

## SQLite / db.py
- WAL mode creates `tracker.db-wal` / `tracker.db-shm` sidecar files — added
  them to .gitignore alongside tracker.db.
- `PRAGMA journal_mode=WAL` returns lowercase `"wal"` — test asserts lowercase.
- Upsert is implemented as SELECT-then-INSERT/UPDATE inside one transaction
  (not ON CONFLICT) because the dupe path must read + merge `found_by_json`
  in Python anyway. Null-preservation via `SET col = COALESCE(?, col)`.
- Connection factory resolves db path: explicit arg > `TRACKER_DB_PATH` env
  var > `./tracker.db`. Tests use tmp_path; stage code should use the default.

## Places API (New) quirks
- **searchText rejects `circle` in `locationRestriction`.** Exact response
  (HTTP 400), captured live 2026-07-07:
  ```json
  {
    "error": {
      "code": 400,
      "message": "Invalid JSON payload received. Unknown name \"circle\" at 'location_restriction': Cannot find field.",
      "status": "INVALID_ARGUMENT",
      "details": [
        {
          "@type": "type.googleapis.com/google.rpc.BadRequest",
          "fieldViolations": [
            {
              "field": "location_restriction",
              "description": "Invalid JSON payload received. Unknown name \"circle\" at 'location_restriction': Cannot find field."
            }
          ]
        }
      ]
    }
  }
  ```
  Circle-restriction only exists on Nearby Search; Text Search allows circle
  only in `locationBias` (soft, can leak far-away results). places.py sends
  the circle's bounding RECTANGLE as the hard restriction and Stage 1's
  shapely circle check trims the corners. **Approved by Kenneth 2026-07-07**;
  SPEC.md Work order 2 amended to match.
- Pagination confirmed live: 20 results/page, `nextPageToken` on pages 1–2,
  page 3 returns no token at the 60 cap. The fresh-token INVALID_REQUEST
  retry (sleep 2s, retry once) is implemented but wasn't triggered in testing.
- `python-dotenv`'s `find_dotenv()` asserts on frame inspection when run via
  stdin/`python -` — pass the path explicitly (`load_dotenv(".env")`) in
  scripts; CLI entrypoints are fine.
- **Text Search results are nondeterministic run-to-run.** Same query minutes
  apart returns slightly different sets (940 vs 954 raw across 2 cells); with
  the 60-cap truncation, ~1-10 marginal venues flip in/out per run. So M2's
  literal "second run new_rows == 0" is unachievable live: observed 247 new →
  10 → 1 → 1 across four runs, a different marginal venue each time. The real
  invariant (zero duplicate place_ids, no data loss, dupes recognized) holds
  exactly. Treat small nonzero new_rows on re-runs as discovery, not a bug.
- Both cocktail_bar cells (river_north, west_loop) hit the 60-cap on at least
  one query → subdivision fires in dense zones; expect it across most tier-1
  cells in the full run.

## Stage 2 / identity matching
- Token overlap needs a naive plural fold (`cocktails` → `cocktail`), or real
  titles like "Machine: Engineered Dining & Cocktails" under-score vs venue
  name "Machine Cocktail Bar". Implemented in `_tokens`; 1-char strip, len>3.
- Domain matching must be substring-on-squashed-host (theviolethour.com
  contains "violet"+"hour"), not token equality — domains concatenate words.
- **SPEC/CLAUDE.md discrepancy, unresolved:** CLAUDE.md Stage 2 says
  `user_ratings_total >= 25`; SPEC.md WO4 says `user_rating_count >= 50`.
  Implemented 50 per SPEC (phase spec governs). Kenneth should reconcile.

## Website fetching / bot detection (2026-07-07 incident: 12 false kills)
- **Restaurant hosting platforms block the Python TLS stack outright.**
  All 12 first-round website_dead kills were false (verified externally by
  Kenneth); 10 clustered on Popmenu and Owner.com. 11/12 cached responses
  were HTTP 403 Cloudflare challenge pages ("Just a moment..."); 1 (Bodega
  Bar, theberkshireroom.com/bodega) was a genuine 404.
- **Headers don't fix it — it's TLS (JA3) fingerprinting.** Evidence: curl
  with byte-identical browser headers gets 200 where httpx gets 403 on both
  HTTP/1.1 and HTTP/2, with and without browser-like cipher ordering. No
  header set can pass; needs a real browser fetch (Playwright, M4) or a
  curl-subprocess fallback (proposed, not implemented).
- Mitigations now in place: full browser header set + one 3s retry on
  403/429/timeout in fetch.py (helps marginal cases, not JA3 blocks);
  403/429 responses are NEVER cached (a cached challenge must not "confirm"
  a second strike); **two-strike website_dead policy** — first failing run
  parks the row at needs_review with marker `website_dead_once` (auto
  re-checked next run), only a second consecutive failing run eliminates.
  SPEC.md WO4 amended to match.
- **Known limitation:** until Playwright (M4), the JA3-blocked sites will
  fail every httpx run — do NOT run stage2 twice in a row expecting them to
  clear, or the second run will (falsely) eliminate them. The 12 are parked
  in the retry queue until M4.
- Mercadito preview of identity-check pitfalls: mercaditorivernorth.com
  301s cross-domain to mercaditorestaurantgroup.com (parent group site).
  This one still passes (squashed domain contains "mercadito"), but venue
  sites that redirect to a group domain NOT containing the venue name will
  land in the 0.2–0.6 needs_review band. Expected, but expect a cluster of
  them for restaurant-group venues.

## Hotel-restaurant blocklist gap (proposal only, NOT implemented)
- 676 Restaurant & Bar is the Omni Chicago Hotel's restaurant, but its
  Places types are purely restaurant/bar (`american_restaurant,
  cocktail_bar, bar, restaurant, food, ...`) — no `lodging` — so the Stage 1
  type blocklist structurally cannot catch hotel restaurants. The only
  reliable signal in our data is the website: it lives under
  omnihotels.com/hotels/chicago/dining/.
- Proposed fix (deterministic, code-decided): add
  `config/hotel_domains.json` with known hotel-brand domains (omnihotels,
  marriott, hilton, hyatt, ihg, fourseasons, langhamhotels, ...). In Stage 2
  after the fetch, if the FINAL redirect domain matches the list, set
  `needs_review` with marker `hotel_restaurant` rather than auto-kill —
  some hotel venues (rooftops, standalone-brand restaurants) are viable
  event spaces, so a human decides. Config is hand-reviewed like zones.json.
  Already-seen candidates in the data: 676/Omni, Holloways Bar (marriott
  .com), Bar Pendry (pendry.com), Upstairs at The Gwen (thegwenchicago.com).

## Decisions made beyond the spec letter (flag to Kenneth if they seem wrong)
- Dupe upsert with a NON-null incoming scalar DOES overwrite the old value
  (spec only forbids null-overwrites; fresher Places data should win).
- An exactly-identical `found_by` entry (same cell_id+query+rank+run_id) is
  NOT appended twice — keeps re-runs idempotent; distinct run_ids still append.
- `upsert_venue` never touches `stage`/`eliminated_reason` on dupes, so a
  re-run of Stage 1 can't resurrect an eliminated row back to 0_raw.
- `set_stage(..., 'eliminated')` without a reason raises ValueError — enforces
  the "every kill has a reason code" invariant at the DB layer.
- Non-mandated modules (places, fetch, llm, stage1, stage2) created as
  docstring-only stubs so the CLAUDE.md repo layout exists without starting
  Work order 2.

## State as of 2026-07-07 (evening)
- Work orders 0–4 complete. `pytest` = 51 passed. Fixture captured from real
  M1 run (3 pages × 20 places).
- M2 done live on west_loop__cocktail_bar + river_north__cocktail_bar:
  259 venues, zero duplicate place_ids across 4 runs. new_rows on re-runs is
  1-10, not 0 — see "Text Search results are nondeterministic" above.
- M3 done live: funnel 259 → 24 rating_below_4, 19 too_few_reviews,
  9 price_level_high, 5 no_website, 1 identity_mismatch → 177 at
  2_filtered_ok, 58 eliminated, 24 needs_review (12 identity-band + 12 in
  the website retry queue after the false-kill incident above). Re-run
  processes 0 fresh rows; 200 fetches cached. M3 acceptance #3 (joint
  10-row spot-check) still owed to Kenneth.
- Deferred/open: robots.txt respect (CLAUDE.md convention) not implemented in
  fetch.py yet — single homepage GETs for now; implement before Stage 3's
  heavier crawling. LLM identity tiebreak for the 12 needs_review rows is a
  later work order. Reconcile the 25-vs-50 review floor (see Stage 2 section).
- Next: Phase 1 definition-of-done wants the M3 funnel reproducible from a
  fresh clone via README, then Phase 2 spec (Stage 3 golden set).
