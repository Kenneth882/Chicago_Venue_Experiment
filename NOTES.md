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

## State as of 2026-07-07
- Work orders 0, 1, 2 complete. `pytest` = 17 passed. M1 acceptance passed
  live: river_north__cocktail_bar query paginated to page 3, 60 results,
  table printed, 3 HTTP requests, 0 DB writes with --no-write.
- `tests/fixtures/places_response.json` captured from the real M1 run
  (3 raw pages, 20 places each) for offline pagination/normalization tests.
- stage1_search.py currently has only the M1 CLI slice; it refuses to run
  without --no-write/--dry-run until Work order 3 lands the write path.
- tracker.db exists with 50 seeded pending cells, 0 venues.
- Next: Work order 3 (full Stage 1: geo check, blocklist, upsert, stats,
  60-cap subdivision — note this query DID hit the 60 cap, so subdivision
  will matter in dense zones).
