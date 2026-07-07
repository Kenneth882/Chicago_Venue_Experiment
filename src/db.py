"""All database access for the venue tracker.

This is the ONLY module allowed to import sqlite3. Every other module goes
through the public functions below, which is what makes the later Supabase
swap a one-file change. DDL is written Postgres-compatible: TEXT/REAL/INTEGER
columns, JSON stored as TEXT, ISO-8601 timestamps as TEXT.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "tracker.db"

# Scalar venue columns that participate in upsert. On conflict, an incoming
# null must never overwrite an existing value (COALESCE semantics).
VENUE_SCALAR_FIELDS = (
    "name",
    "formatted_address",
    "lat",
    "lng",
    "zone_id",
    "rating",
    "user_rating_count",
    "price_level",
    "business_status",
    "website_uri",
    "types_json",
    "primary_type",
)

_SCHEMA = """
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
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Connection factory. WAL mode, row access by column name.

    Resolution order: explicit arg > TRACKER_DB_PATH env var > ./tracker.db.
    """
    path = Path(db_path or os.environ.get("TRACKER_DB_PATH", DEFAULT_DB_PATH))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist. Idempotent."""
    with conn:
        conn.executescript(_SCHEMA)


def seed_cells(
    conn: sqlite3.Connection,
    zones: list[dict[str, Any]],
    venue_types: list[dict[str, Any]],
) -> int:
    """Insert one cell per zone x venue_type. Idempotent; returns cells added."""
    now = _utc_now()
    added = 0
    with conn:
        for zone in zones:
            for vt in venue_types:
                cell_id = f"{zone['zone_id']}__{vt['type_id']}"
                cur = conn.execute(
                    "INSERT OR IGNORE INTO cells (cell_id, zone_id, type_id, status, updated_at) "
                    "VALUES (?, ?, ?, 'pending', ?)",
                    (cell_id, zone["zone_id"], vt["type_id"], now),
                )
                added += cur.rowcount
    return added


def claim_pending_cell(conn: sqlite3.Connection, run_id: str) -> Optional[str]:
    """Atomically claim one pending cell for this run. Returns cell_id or None."""
    with conn:
        row = conn.execute(
            "UPDATE cells SET status = 'running', run_id = ?, updated_at = ? "
            "WHERE cell_id = ("
            "  SELECT cell_id FROM cells WHERE status = 'pending' ORDER BY cell_id LIMIT 1"
            ") RETURNING cell_id",
            (run_id, _utc_now()),
        ).fetchone()
    return row["cell_id"] if row else None


def complete_cell(conn: sqlite3.Connection, cell_id: str, status: str) -> None:
    """Mark a cell done/failed (or reset to pending)."""
    if status not in ("pending", "running", "done", "failed"):
        raise ValueError(f"invalid cell status: {status!r}")
    with conn:
        conn.execute(
            "UPDATE cells SET status = ?, updated_at = ? WHERE cell_id = ?",
            (status, _utc_now(), cell_id),
        )


def mark_cell_running(conn: sqlite3.Connection, cell_id: str, run_id: str) -> None:
    """Stamp a specific cell as running under this run_id (explicit CLI selection,
    as opposed to claim_pending_cell's claim-any semantics)."""
    with conn:
        conn.execute(
            "UPDATE cells SET status = 'running', run_id = ?, updated_at = ? WHERE cell_id = ?",
            (run_id, _utc_now(), cell_id),
        )


def get_venue(conn: sqlite3.Connection, place_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM venues WHERE place_id = ?", (place_id,)
    ).fetchone()


def promote_stage(
    conn: sqlite3.Connection, place_id: str, to_stage: str, from_stage: str
) -> bool:
    """Set stage only if the row is currently at from_stage. Returns True if
    promoted. This is how Stage 1 marks survivors 1_geo_ok without
    resurrecting eliminated/filtered rows on re-runs."""
    with conn:
        cur = conn.execute(
            "UPDATE venues SET stage = ?, updated_at = ? WHERE place_id = ? AND stage = ?",
            (to_stage, _utc_now(), place_id, from_stage),
        )
    return cur.rowcount > 0


def venues_at_stage(
    conn: sqlite3.Connection, stage: str, limit: Optional[int] = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM venues WHERE stage = ? ORDER BY place_id"
    if limit is not None:
        return conn.execute(sql + " LIMIT ?", (stage, limit)).fetchall()
    return conn.execute(sql, (stage,)).fetchall()


def venues_flagged(
    conn: sqlite3.Connection, stage: str, reason: str
) -> list[sqlite3.Row]:
    """Rows at a stage carrying a specific marker in eliminated_reason
    (e.g. needs_review + website_dead_once = the website retry queue)."""
    return conn.execute(
        "SELECT * FROM venues WHERE stage = ? AND eliminated_reason = ? ORDER BY place_id",
        (stage, reason),
    ).fetchall()


def upsert_venue(conn: sqlite3.Connection, record: dict[str, Any]) -> str:
    """Insert or update a venue keyed on place_id. Returns "new" or "dupe".

    record must contain: place_id, name, found_by (a single provenance entry
    dict: {cell_id, query, rank, run_id}). Any scalar column from
    VENUE_SCALAR_FIELDS may be present; a "types" list is serialized to
    types_json.

    Dupe semantics: incoming nulls never overwrite existing values; the
    found_by entry is appended (unless already present verbatim);
    updated_at is bumped; stage/eliminated_reason are never touched here.
    """
    record = dict(record)
    if "types" in record and "types_json" not in record:
        record["types_json"] = json.dumps(record.pop("types"))
    place_id = record["place_id"]
    entry = record["found_by"]
    now = _utc_now()

    with conn:
        existing = conn.execute(
            "SELECT found_by_json FROM venues WHERE place_id = ?", (place_id,)
        ).fetchone()

        if existing is None:
            conn.execute(
                f"INSERT INTO venues (place_id, {', '.join(VENUE_SCALAR_FIELDS)}, "
                "found_by_json, first_seen_at, updated_at) "
                f"VALUES ({', '.join('?' * (len(VENUE_SCALAR_FIELDS) + 4))})",
                (
                    place_id,
                    *(record.get(f) for f in VENUE_SCALAR_FIELDS),
                    json.dumps([entry]),
                    now,
                    now,
                ),
            )
            return "new"

        found_by = json.loads(existing["found_by_json"])
        if entry not in found_by:
            found_by.append(entry)
        assignments = ", ".join(f"{f} = COALESCE(?, {f})" for f in VENUE_SCALAR_FIELDS)
        conn.execute(
            f"UPDATE venues SET {assignments}, found_by_json = ?, updated_at = ? "
            "WHERE place_id = ?",
            (
                *(record.get(f) for f in VENUE_SCALAR_FIELDS),
                json.dumps(found_by),
                now,
                place_id,
            ),
        )
        return "dupe"


def append_found_by(conn: sqlite3.Connection, place_id: str, entry: dict[str, Any]) -> None:
    """Append one provenance entry to a venue's found_by_json (skips exact dupes)."""
    with conn:
        row = conn.execute(
            "SELECT found_by_json FROM venues WHERE place_id = ?", (place_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no venue with place_id {place_id!r}")
        found_by = json.loads(row["found_by_json"])
        if entry in found_by:
            return
        found_by.append(entry)
        conn.execute(
            "UPDATE venues SET found_by_json = ?, updated_at = ? WHERE place_id = ?",
            (json.dumps(found_by), _utc_now(), place_id),
        )


def set_stage(
    conn: sqlite3.Connection,
    place_id: str,
    stage: str,
    reason: Optional[str] = None,
) -> None:
    """Set a venue's stage; reason is required iff stage == 'eliminated'."""
    if stage == "eliminated" and not reason:
        raise ValueError("eliminated rows must carry an eliminated_reason")
    with conn:
        cur = conn.execute(
            "UPDATE venues SET stage = ?, eliminated_reason = ?, updated_at = ? "
            "WHERE place_id = ?",
            (stage, reason, _utc_now(), place_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"no venue with place_id {place_id!r}")


def write_extraction(
    conn: sqlite3.Connection, place_id: str, extraction: dict[str, Any]
) -> None:
    """Store the Stage 3 extraction envelope (JSON) on a venue."""
    with conn:
        cur = conn.execute(
            "UPDATE venues SET extraction_json = ?, updated_at = ? WHERE place_id = ?",
            (json.dumps(extraction), _utc_now(), place_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"no venue with place_id {place_id!r}")


def write_stats(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Append one per-cell run_stats row."""
    with conn:
        conn.execute(
            "INSERT INTO run_stats (run_id, cell_id, raw_results, killed_geo, "
            "killed_type, new_rows, dupes, hit_60_cap, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["run_id"],
                row["cell_id"],
                row.get("raw_results"),
                row.get("killed_geo"),
                row.get("killed_type"),
                row.get("new_rows"),
                row.get("dupes"),
                row.get("hit_60_cap"),
                _utc_now(),
            ),
        )
