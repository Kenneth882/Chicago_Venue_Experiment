"""Tests for src/db.py — schema, seeding, cell claiming, upsert semantics.

No live APIs, no network: everything runs against a temp SQLite file.
"""

import json
from pathlib import Path

import pytest

from src import db

ZONES = json.loads((Path(__file__).parent.parent / "config" / "zones.json").read_text())["zones"]
VENUE_TYPES = json.loads(
    (Path(__file__).parent.parent / "config" / "venue_types.json").read_text()
)["venue_types"]


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_db(conn)
    yield conn
    conn.close()


def make_record(**overrides):
    record = {
        "place_id": "ChIJtest123",
        "name": "The Violet Hour",
        "formatted_address": "1520 N Damen Ave, Chicago, IL 60622",
        "lat": 41.9089,
        "lng": -87.6773,
        "zone_id": "wicker_park",
        "rating": 4.6,
        "user_rating_count": 2100,
        "price_level": 3,
        "business_status": "OPERATIONAL",
        "website_uri": "https://theviolethour.com",
        "types": ["bar", "point_of_interest"],
        "primary_type": "bar",
        "found_by": {
            "cell_id": "wicker_park__cocktail_bar",
            "query": "cocktail bar Wicker Park / Bucktown Chicago",
            "rank": 1,
            "run_id": "20260707-120000",
        },
    }
    record.update(overrides)
    return record


def get_venue(conn, place_id="ChIJtest123"):
    return conn.execute("SELECT * FROM venues WHERE place_id = ?", (place_id,)).fetchone()


# --- schema / init ---

def test_init_db_creates_tables_and_is_idempotent(conn):
    db.init_db(conn)  # second call must not fail
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"venues", "cells", "run_stats"} <= tables


def test_wal_mode_enabled(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# --- seed_cells ---

def test_seed_cells_creates_50_and_is_idempotent(conn):
    assert db.seed_cells(conn, ZONES, VENUE_TYPES) == 50
    assert db.seed_cells(conn, ZONES, VENUE_TYPES) == 0
    assert conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 50
    row = conn.execute(
        "SELECT * FROM cells WHERE cell_id = 'river_north__cocktail_bar'"
    ).fetchone()
    assert row["zone_id"] == "river_north"
    assert row["type_id"] == "cocktail_bar"
    assert row["status"] == "pending"


# --- claim / complete cells ---

def test_claim_and_complete_cell(conn):
    db.seed_cells(conn, ZONES, VENUE_TYPES)
    cell_id = db.claim_pending_cell(conn, "20260707-120000")
    assert cell_id is not None
    row = conn.execute("SELECT * FROM cells WHERE cell_id = ?", (cell_id,)).fetchone()
    assert row["status"] == "running"
    assert row["run_id"] == "20260707-120000"

    db.complete_cell(conn, cell_id, "done")
    row = conn.execute("SELECT * FROM cells WHERE cell_id = ?", (cell_id,)).fetchone()
    assert row["status"] == "done"


def test_claim_never_returns_same_cell_twice(conn):
    db.seed_cells(conn, ZONES, VENUE_TYPES)
    claimed = set()
    while (cell_id := db.claim_pending_cell(conn, "r1")) is not None:
        assert cell_id not in claimed
        claimed.add(cell_id)
    assert len(claimed) == 50


def test_complete_cell_rejects_bad_status(conn):
    with pytest.raises(ValueError):
        db.complete_cell(conn, "any", "finished")


# --- upsert_venue ---

def test_upsert_new_venue(conn):
    assert db.upsert_venue(conn, make_record()) == "new"
    row = get_venue(conn)
    assert row["name"] == "The Violet Hour"
    assert row["stage"] == "0_raw"
    assert row["rating"] == 4.6
    assert json.loads(row["types_json"]) == ["bar", "point_of_interest"]
    found_by = json.loads(row["found_by_json"])
    assert len(found_by) == 1
    assert found_by[0]["cell_id"] == "wicker_park__cocktail_bar"
    assert row["first_seen_at"] == row["updated_at"]


def test_upsert_dupe_returns_dupe_appends_found_by_and_keeps_scalars(conn):
    """The core invariant: second upsert of the same place_id returns "dupe",
    appends the new found_by entry, and never overwrites scalars with nulls."""
    db.upsert_venue(conn, make_record())

    second = make_record(
        # nulls for almost every scalar — none may clobber existing values
        formatted_address=None,
        lat=None,
        lng=None,
        zone_id=None,
        rating=None,
        user_rating_count=None,
        price_level=None,
        business_status=None,
        website_uri=None,
        primary_type=None,
        found_by={
            "cell_id": "wicker_park__rooftop",
            "query": "rooftop bar Wicker Park / Bucktown Chicago",
            "rank": 7,
            "run_id": "20260707-130000",
        },
    )
    second.pop("types")

    assert db.upsert_venue(conn, second) == "dupe"
    assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 1

    row = get_venue(conn)
    # scalars survived the null-heavy second record
    assert row["rating"] == 4.6
    assert row["user_rating_count"] == 2100
    assert row["price_level"] == 3
    assert row["website_uri"] == "https://theviolethour.com"
    assert row["business_status"] == "OPERATIONAL"
    assert row["formatted_address"] == "1520 N Damen Ave, Chicago, IL 60622"
    assert json.loads(row["types_json"]) == ["bar", "point_of_interest"]
    # found_by appended, in order
    found_by = json.loads(row["found_by_json"])
    assert [e["cell_id"] for e in found_by] == [
        "wicker_park__cocktail_bar",
        "wicker_park__rooftop",
    ]


def test_upsert_dupe_nonnull_values_do_update(conn):
    db.upsert_venue(conn, make_record(rating=4.4))
    db.upsert_venue(conn, make_record(rating=4.7, found_by=make_record()["found_by"]))
    assert get_venue(conn)["rating"] == 4.7


def test_upsert_identical_found_by_not_duplicated(conn):
    record = make_record()
    db.upsert_venue(conn, record)
    assert db.upsert_venue(conn, make_record()) == "dupe"
    assert len(json.loads(get_venue(conn)["found_by_json"])) == 1


def test_upsert_never_creates_duplicate_place_ids(conn):
    for _ in range(3):
        db.upsert_venue(conn, make_record())
    dupes = conn.execute(
        "SELECT place_id, COUNT(*) FROM venues GROUP BY place_id HAVING COUNT(*) > 1"
    ).fetchall()
    assert dupes == []


def test_upsert_does_not_touch_stage(conn):
    db.upsert_venue(conn, make_record())
    db.set_stage(conn, "ChIJtest123", "1_geo_ok")
    db.upsert_venue(conn, make_record())
    assert get_venue(conn)["stage"] == "1_geo_ok"


# --- append_found_by ---

def test_append_found_by(conn):
    db.upsert_venue(conn, make_record())
    db.append_found_by(
        conn,
        "ChIJtest123",
        {"cell_id": "wicker_park__brewery", "query": "q", "rank": 2, "run_id": "r2"},
    )
    assert len(json.loads(get_venue(conn)["found_by_json"])) == 2


def test_append_found_by_missing_venue_raises(conn):
    with pytest.raises(KeyError):
        db.append_found_by(conn, "nope", {"cell_id": "c", "query": "q", "rank": 1, "run_id": "r"})


# --- set_stage ---

def test_set_stage_eliminated_requires_reason(conn):
    db.upsert_venue(conn, make_record())
    with pytest.raises(ValueError):
        db.set_stage(conn, "ChIJtest123", "eliminated")
    db.set_stage(conn, "ChIJtest123", "eliminated", reason="rating_below_4")
    row = get_venue(conn)
    assert row["stage"] == "eliminated"
    assert row["eliminated_reason"] == "rating_below_4"


def test_set_stage_missing_venue_raises(conn):
    with pytest.raises(KeyError):
        db.set_stage(conn, "nope", "1_geo_ok")


# --- promote_stage / venues_at_stage / get_venue ---

def test_promote_stage_only_from_matching_stage(conn):
    db.upsert_venue(conn, make_record())
    assert db.promote_stage(conn, "ChIJtest123", "1_geo_ok", from_stage="0_raw") is True
    assert get_venue(conn)["stage"] == "1_geo_ok"
    # a second promote from 0_raw is a no-op
    assert db.promote_stage(conn, "ChIJtest123", "1_geo_ok", from_stage="0_raw") is False
    # an eliminated row cannot be resurrected by promote
    db.set_stage(conn, "ChIJtest123", "eliminated", reason="rating_below_4")
    assert db.promote_stage(conn, "ChIJtest123", "1_geo_ok", from_stage="0_raw") is False
    assert get_venue(conn)["stage"] == "eliminated"


def test_venues_at_stage_filters_and_limits(conn):
    for i in range(3):
        db.upsert_venue(conn, make_record(place_id=f"ChIJ{i}"))
    db.set_stage(conn, "ChIJ0", "1_geo_ok")
    db.set_stage(conn, "ChIJ1", "1_geo_ok")
    assert len(db.venues_at_stage(conn, "1_geo_ok")) == 2
    assert len(db.venues_at_stage(conn, "1_geo_ok", limit=1)) == 1
    assert len(db.venues_at_stage(conn, "2_filtered_ok")) == 0


def test_get_venue(conn):
    assert db.get_venue(conn, "missing") is None
    db.upsert_venue(conn, make_record())
    assert db.get_venue(conn, "ChIJtest123")["name"] == "The Violet Hour"


def test_mark_cell_running(conn):
    db.seed_cells(conn, ZONES, VENUE_TYPES)
    db.mark_cell_running(conn, "loop__brewery", "r9")
    row = conn.execute("SELECT * FROM cells WHERE cell_id = 'loop__brewery'").fetchone()
    assert row["status"] == "running"
    assert row["run_id"] == "r9"


# --- write_stats ---

def test_write_stats(conn):
    db.write_stats(
        conn,
        {
            "run_id": "20260707-120000",
            "cell_id": "river_north__cocktail_bar",
            "raw_results": 42,
            "killed_geo": 3,
            "killed_type": 2,
            "new_rows": 35,
            "dupes": 2,
            "hit_60_cap": 0,
        },
    )
    row = conn.execute("SELECT * FROM run_stats").fetchone()
    assert row["raw_results"] == 42
    assert row["hit_60_cap"] == 0
    assert row["created_at"]
