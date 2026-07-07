# Chicago Venue Sourcing Agent

Setup: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`, then `cp .env.example .env` and fill in keys.

- DB layer tests: `pytest tests/test_db.py`
- M1 check: `python -m src.stage1_search --cell river_north__cocktail_bar --limit 1 --no-write`
- M2 check: `python -m src.stage1_search --cell west_loop__cocktail_bar --cell river_north__cocktail_bar` (run twice; second run must add 0 new rows)
- M3 check: `python -m src.stage2_filter` then `pytest tests/`
