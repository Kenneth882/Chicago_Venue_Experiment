"""Stage 1 search workers — Work order 3 (Milestone 2).

CLI: python -m src.stage1_search [--cell CELL_ID ... | --zone ZONE_ID | --all]
     [--dry-run] [--limit N] [--no-write]

Per cell: run its frozen queries (with {zone} substituted) through
places.search_text. Per result: shapely point-in-circle check against the
zone (killed_geo), type blocklist (killed_type), normalize, upsert with a
found_by provenance entry, promote 0_raw -> 1_geo_ok. If a query returns the
full 60 results, subdivide the cell circle into 4 half-radius sub-circles and
re-run that query once (one recursion level). One run_stats row per cell.

--dry-run: zero HTTP calls, zero DB writes. --no-write: real HTTP, zero DB
writes. --limit N caps the number of top-level queries executed in total.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from shapely.geometry import Point

from src import db, places

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

BLOCKED_TYPES = {"lodging", "casino", "liquor_store"}

# Places API (New) returns priceLevel as an enum string.
PRICE_LEVEL_MAP = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}

METERS_PER_DEG_LAT = 111_320.0

STAT_KEYS = ("raw_results", "killed_geo", "killed_type", "new_rows", "dupes", "hit_60_cap")


# --- pure logic (unit-tested offline) ---

def in_zone(lat: float, lng: float, center: dict[str, float], radius_m: float) -> bool:
    """Circle check: shapely distance from zone center <= radius_m, in a local
    equirectangular meter projection centered on the zone."""
    x = (lng - center["lng"]) * METERS_PER_DEG_LAT * math.cos(math.radians(center["lat"]))
    y = (lat - center["lat"]) * METERS_PER_DEG_LAT
    return Point(x, y).distance(Point(0.0, 0.0)) <= radius_m


def is_blocked_type(types: Optional[list[str]], primary_type: Optional[str]) -> bool:
    ts = set(types or [])
    if ts & BLOCKED_TYPES:
        return True
    if primary_type == "night_club" and not (ts & {"bar", "restaurant"}):
        return True
    return False


def normalize_place(place: dict[str, Any], zone_id: str) -> dict[str, Any]:
    """Raw Places API dict -> venues schema fields."""
    loc = place.get("location") or {}
    return {
        "place_id": place["id"],
        "name": (place.get("displayName") or {}).get("text") or place["id"],
        "formatted_address": place.get("formattedAddress"),
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "zone_id": zone_id,
        "rating": place.get("rating"),
        "user_rating_count": place.get("userRatingCount"),
        "price_level": PRICE_LEVEL_MAP.get(place.get("priceLevel")),
        "business_status": place.get("businessStatus"),
        "website_uri": place.get("websiteUri"),
        "types": place.get("types") or [],
        "primary_type": place.get("primaryType"),
    }


def sub_circles(center: dict[str, float], radius_m: float) -> list[tuple[dict[str, float], float]]:
    """Four half-radius circles, centers offset +/- radius/2 in lat and lng."""
    half = radius_m / 2.0
    dlat = half / METERS_PER_DEG_LAT
    dlng = half / (METERS_PER_DEG_LAT * math.cos(math.radians(center["lat"])))
    return [
        ({"lat": center["lat"] + sy * dlat, "lng": center["lng"] + sx * dlng}, half)
        for sy in (1, -1)
        for sx in (1, -1)
    ]


def load_cells() -> dict[str, dict[str, Any]]:
    """cell_id -> {zone, venue_type, queries} from frozen config."""
    zones = json.loads((CONFIG_DIR / "zones.json").read_text())["zones"]
    venue_types = json.loads((CONFIG_DIR / "venue_types.json").read_text())["venue_types"]
    cells = {}
    for zone in zones:
        for vt in venue_types:
            cells[f"{zone['zone_id']}__{vt['type_id']}"] = {
                "zone": zone,
                "venue_type": vt,
                "queries": [q.replace("{zone}", zone["name"]) for q in vt["queries"]],
            }
    return cells


# --- per-cell worker ---

def _process_results(
    conn: Optional[Any],
    results: list[dict[str, Any]],
    *,
    cell_id: str,
    zone: dict[str, Any],
    query: str,
    run_id: str,
    sub: Optional[int],
    write: bool,
    stats: dict[str, int],
) -> None:
    for rank, place in enumerate(results, start=1):
        stats["raw_results"] += 1
        norm = normalize_place(place, zone["zone_id"])
        if (
            norm["lat"] is None
            or norm["lng"] is None
            or not in_zone(norm["lat"], norm["lng"], zone["center"], zone["radius_m"])
        ):
            stats["killed_geo"] += 1
            logger.info("killed_geo | %s | %s", norm["place_id"], norm["name"])
            continue
        if is_blocked_type(norm["types"], norm["primary_type"]):
            stats["killed_type"] += 1
            logger.info(
                "killed_type | %s | %s | primary=%s", norm["place_id"], norm["name"],
                norm["primary_type"],
            )
            continue
        if not write:
            continue
        entry: dict[str, Any] = {"cell_id": cell_id, "query": query, "rank": rank, "run_id": run_id}
        if sub is not None:
            entry["sub"] = sub
        status = db.upsert_venue(conn, {**norm, "found_by": entry})
        stats["new_rows" if status == "new" else "dupes"] += 1
        db.promote_stage(conn, norm["place_id"], "1_geo_ok", from_stage="0_raw")


def process_cell(
    conn: Optional[Any],
    cell_id: str,
    cell: dict[str, Any],
    run_id: str,
    *,
    dry_run: bool,
    write: bool,
    query_budget: Optional[list[int]] = None,
) -> dict[str, int]:
    zone, vt = cell["zone"], cell["venue_type"]
    stats = {k: 0 for k in STAT_KEYS}

    if write:
        db.mark_cell_running(conn, cell_id, run_id)

    try:
        for query in cell["queries"]:
            if query_budget is not None:
                if query_budget[0] <= 0:
                    break
                query_budget[0] -= 1
            results = places.search_text(
                query, zone["center"], zone["radius_m"],
                included_type=vt["included_type"], dry_run=dry_run,
            )
            _process_results(
                conn, results, cell_id=cell_id, zone=zone, query=query,
                run_id=run_id, sub=None, write=write, stats=stats,
            )
            if len(results) >= 60:
                stats["hit_60_cap"] = 1
                logger.info("60-cap hit: cell=%s query=%r — subdividing into 4", cell_id, query)
                for i, (sub_center, sub_radius) in enumerate(
                    sub_circles(zone["center"], zone["radius_m"]), start=1
                ):
                    sub_results = places.search_text(
                        query, sub_center, sub_radius,
                        included_type=vt["included_type"], dry_run=dry_run,
                    )
                    _process_results(
                        conn, sub_results, cell_id=cell_id, zone=zone, query=query,
                        run_id=run_id, sub=i, write=write, stats=stats,
                    )
    except Exception:
        if write:
            db.complete_cell(conn, cell_id, "failed")
        raise

    if write:
        db.write_stats(conn, {"run_id": run_id, "cell_id": cell_id, **stats})
        db.complete_cell(conn, cell_id, "done")
    return stats


# --- CLI ---

def print_stats_table(all_stats: dict[str, dict[str, int]]) -> None:
    header = (f"{'cell_id':<32} {'raw':>5} {'geo✗':>5} {'type✗':>6} "
              f"{'new':>5} {'dupes':>6} {'cap':>4}")
    print("\n" + header)
    print("-" * len(header))
    totals = {k: 0 for k in STAT_KEYS}
    for cell_id, s in all_stats.items():
        print(f"{cell_id:<32} {s['raw_results']:>5} {s['killed_geo']:>5} "
              f"{s['killed_type']:>6} {s['new_rows']:>5} {s['dupes']:>6} {s['hit_60_cap']:>4}")
        for k in STAT_KEYS:
            totals[k] += s[k]
    print("-" * len(header))
    print(f"{'TOTAL':<32} {totals['raw_results']:>5} {totals['killed_geo']:>5} "
          f"{totals['killed_type']:>6} {totals['new_rows']:>5} {totals['dupes']:>6} "
          f"{totals['hit_60_cap']:>4}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 1 search workers")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--cell", action="append", help="cell_id, repeatable")
    group.add_argument("--zone", help="run all cells of one zone_id")
    group.add_argument("--all", action="store_true", help="run all 50 cells")
    parser.add_argument("--limit", type=int, default=None, help="max top-level queries in total")
    parser.add_argument("--dry-run", action="store_true", help="zero HTTP calls, zero DB writes")
    parser.add_argument("--no-write", action="store_true", help="real HTTP, zero DB writes")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    write = not (args.dry_run or args.no_write)

    cells = load_cells()
    if args.all:
        selected = list(cells)
    elif args.zone:
        selected = [c for c in cells if c.startswith(f"{args.zone}__")]
        if not selected:
            parser.error(f"unknown zone_id {args.zone!r}")
    else:
        for cell_id in args.cell:
            if cell_id not in cells:
                parser.error(f"unknown cell_id {cell_id!r}")
        selected = args.cell

    conn = None
    if write:
        conn = db.connect()
        db.init_db(conn)
        zones = json.loads((CONFIG_DIR / "zones.json").read_text())["zones"]
        venue_types = json.loads((CONFIG_DIR / "venue_types.json").read_text())["venue_types"]
        db.seed_cells(conn, zones, venue_types)

    query_budget = [args.limit] if args.limit is not None else None
    all_stats: dict[str, dict[str, int]] = {}
    for cell_id in selected:
        if query_budget is not None and query_budget[0] <= 0:
            break
        logger.info("run_id=%s cell=%s starting", run_id, cell_id)
        all_stats[cell_id] = process_cell(
            conn, cell_id, cells[cell_id], run_id, dry_run=args.dry_run, write=write,
        )

    print_stats_table(all_stats)
    print(f"\nrun_id: {run_id} | write={'yes' if write else 'NO'} | "
          f"HTTP requests made: {places.request_count}")
    if conn is not None:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
