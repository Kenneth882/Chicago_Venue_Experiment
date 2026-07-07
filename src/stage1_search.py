"""Stage 1 search workers.

Milestone 1 scope (current): --cell + --limit + --no-write/--dry-run, run the
cell's frozen queries through places.search_text, print a results table.

Milestone 2 scope (Work order 3, NOT yet implemented): geo check, type
blocklist, normalize, upsert, per-cell run_stats, 60-cap subdivision,
--zone/--all. Until then this module refuses to run without --no-write or
--dry-run so it can never write the DB in a half-built state.

CLI: python -m src.stage1_search [--cell CELL_ID ...] [--dry-run] [--limit N] [--no-write]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src import places

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_cells() -> dict[str, dict[str, Any]]:
    """Build cell_id -> {zone, venue_type, queries} from frozen config."""
    zones = json.loads((CONFIG_DIR / "zones.json").read_text())["zones"]
    venue_types = json.loads((CONFIG_DIR / "venue_types.json").read_text())["venue_types"]
    cells = {}
    for zone in zones:
        for vt in venue_types:
            cell_id = f"{zone['zone_id']}__{vt['type_id']}"
            cells[cell_id] = {
                "zone": zone,
                "venue_type": vt,
                "queries": [q.replace("{zone}", zone["name"]) for q in vt["queries"]],
            }
    return cells


def print_results_table(results: list[dict[str, Any]]) -> None:
    header = f"{'#':>3}  {'name':<38} {'rating':>6} {'reviews':>7}  {'website':<44} place_id"
    print(header)
    print("-" * len(header))
    for rank, place in enumerate(results, start=1):
        name = (place.get("displayName") or {}).get("text", "?")[:38]
        rating = place.get("rating", "")
        reviews = place.get("userRatingCount", "")
        website = (place.get("websiteUri") or "")[:44]
        print(f"{rank:>3}  {name:<38} {rating:>6} {reviews:>7}  {website:<44} {place.get('id', '?')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 1 search workers (M1 scope)")
    parser.add_argument("--cell", action="append", default=[], help="cell_id, repeatable")
    parser.add_argument("--limit", type=int, default=None, help="max queries to run in total")
    parser.add_argument("--dry-run", action="store_true", help="log requests, make zero HTTP calls")
    parser.add_argument("--no-write", action="store_true", help="print results, skip all DB writes")
    parser.add_argument("--capture-fixture", metavar="PATH", default=None,
                        help="save raw response pages of the first query to PATH (JSON)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()

    if not (args.no_write or args.dry_run):
        parser.error(
            "DB writes are not implemented until Work order 3 — pass --no-write (or --dry-run)"
        )
    if not args.cell:
        parser.error("pass at least one --cell CELL_ID (e.g. river_north__cocktail_bar)")

    cells = load_cells()
    for cell_id in args.cell:
        if cell_id not in cells:
            parser.error(f"unknown cell_id {cell_id!r}")

    queries_run = 0
    fixture_pages: list[dict[str, Any]] | None = [] if args.capture_fixture else None

    for cell_id in args.cell:
        cell = cells[cell_id]
        zone, vt = cell["zone"], cell["venue_type"]
        for query in cell["queries"]:
            if args.limit is not None and queries_run >= args.limit:
                break
            queries_run += 1
            print(f"\n=== cell={cell_id} query={query!r} "
                  f"included_type={vt['included_type']} ===")
            results = places.search_text(
                query,
                zone["center"],
                zone["radius_m"],
                included_type=vt["included_type"],
                dry_run=args.dry_run,
                page_sink=fixture_pages if queries_run == 1 else None,
            )
            if args.dry_run:
                print("(dry run — no results)")
            else:
                print_results_table(results)
                print(f"total: {len(results)} results")

    if args.capture_fixture and fixture_pages:
        Path(args.capture_fixture).parent.mkdir(parents=True, exist_ok=True)
        Path(args.capture_fixture).write_text(json.dumps(fixture_pages, indent=2))
        print(f"\nsaved {len(fixture_pages)} raw response page(s) to {args.capture_fixture}")

    print(f"\nqueries run: {queries_run} | HTTP requests made: {places.request_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
