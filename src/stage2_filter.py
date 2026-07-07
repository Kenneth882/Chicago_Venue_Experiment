"""Stage 2 cheap filter gate — Work order 4 (Milestone 3).

CLI: python -m src.stage2_filter [--limit N] [--dry-run]

Operates on all venues at stage 1_geo_ok. Checks run in this exact order,
stopping at the first failure; each failure sets stage='eliminated' with a
reason code:

  1. business_status == OPERATIONAL          else not_operational
  2. rating >= 4.0                           else rating_below_4
     user_rating_count >= 50                 else too_few_reviews
  3. price_level null or <= 3                else price_level_high
  4. website_uri present                     else no_website
  5. website fetch returns HTTP 200          else two-strike policy:
     first failing run  -> needs_review + marker 'website_dead_once'
     second consecutive failing run -> eliminated / website_dead
     (bot-blocked sites recover instead of dying on one bad fetch)
  6. identity token-overlap score:
     >= 0.6 pass | <= 0.2 website_identity_mismatch | middle -> needs_review
     (No Claude tiebreak in this phase.)

Each run also re-checks the website retry queue (needs_review rows carrying
the website_dead_once marker). Survivors -> stage='2_filtered_ok'. Every
elimination logged at INFO: place_id | name | reason.

--dry-run: no network, no DB writes — evaluates the offline checks (1-4)
and reports how many rows would go on to the website checks.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import unicodedata
from collections import Counter
from typing import Any, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from src import db, fetch

logger = logging.getLogger(__name__)

PASS_SCORE = 0.6
FAIL_SCORE = 0.2

# Tokens too generic to identify a venue; dropped from names before scoring
# (with a fallback if the whole name is generic).
GENERIC_TOKENS = {
    "the", "a", "an", "and", "of", "at", "on", "in",
    "chicago", "il", "bar", "restaurant", "lounge", "tavern", "grill",
    "kitchen", "room", "club", "cafe", "co", "inc", "llc",
}


# --- checks 1-4: offline, on data Stage 1 already fetched ---

def offline_check(venue: Any) -> Optional[str]:
    """Returns the elimination reason, or None if checks 1-4 all pass."""
    if venue["business_status"] != "OPERATIONAL":
        return "not_operational"
    if venue["rating"] is None or venue["rating"] < 4.0:
        return "rating_below_4"
    if venue["user_rating_count"] is None or venue["user_rating_count"] < 50:
        return "too_few_reviews"
    if venue["price_level"] is not None and venue["price_level"] > 3:
        return "price_level_high"
    if not venue["website_uri"]:
        return "no_website"
    return None


# --- check 6: identity match (pure, unit-tested) ---

def _singular(t: str) -> str:
    # naive plural fold so "cocktails" matches "cocktail"
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def _tokens(s: Optional[str]) -> set[str]:
    if not s:
        return set()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return {_singular(t) for t in re.findall(r"[a-z0-9]+", s.lower())}


def name_tokens(name: str) -> set[str]:
    toks = {t for t in _tokens(name) - GENERIC_TOKENS if len(t) >= 2}
    return toks or _tokens(name)


def identity_score(
    venue_name: str,
    final_url: Optional[str],
    title: Optional[str],
    og_site_name: Optional[str],
) -> float:
    """Fraction of the venue's name tokens found in the final domain, <title>,
    or og:site_name. Domain matching is substring-based on the squashed host
    (theviolethour.com contains 'violet' and 'hour')."""
    vt = name_tokens(venue_name)
    if not vt:
        return 0.0
    text_tokens = _tokens(title) | _tokens(og_site_name)
    host = (urlparse(final_url or "").netloc or "").lower()
    host = host.removeprefix("www.")
    squashed = re.sub(r"[^a-z0-9]", "", host.rsplit(".", 1)[0] if "." in host else host)
    hits = sum(
        1 for t in vt if t in text_tokens or (len(t) >= 3 and t in squashed)
    )
    return hits / len(vt)


def identity_verdict(score: float) -> str:
    if score >= PASS_SCORE:
        return "pass"
    if score <= FAIL_SCORE:
        return "mismatch"
    return "review"


def extract_identity_fields(html: str) -> tuple[str, str]:
    """(title, og:site_name) from a homepage."""
    soup = BeautifulSoup(html or "", "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    og = soup.find("meta", attrs={"property": "og:site_name"})
    og_name = (og.get("content") or "").strip() if og else ""
    return title, og_name


# --- runner ---

def run(conn: Any, *, limit: Optional[int] = None, dry_run: bool = False) -> Counter:
    rows = db.venues_at_stage(conn, "1_geo_ok", limit)
    retry_queue = db.venues_flagged(conn, "needs_review", "website_dead_once")
    funnel: Counter = Counter()
    funnel["in_1_geo_ok"] = len(rows)
    funnel["in_website_retry_queue"] = len(retry_queue)

    if dry_run:
        rows_to_process = rows
    else:
        rows_to_process = list(rows) + list(retry_queue)

    for v in rows_to_process:
        reason = offline_check(v)
        if reason:
            funnel[reason] += 1
            logger.info("%s | %s | %s", v["place_id"], v["name"], reason)
            if not dry_run:
                db.set_stage(conn, v["place_id"], "eliminated", reason)
            continue

        if dry_run:
            funnel["would_check_website"] += 1
            continue

        resp = fetch.get(v["website_uri"])
        if resp.status != 200:
            if v["eliminated_reason"] == "website_dead_once":
                # second consecutive failed run — now it's a kill
                funnel["website_dead"] += 1
                logger.info(
                    "%s | %s | website_dead (2nd strike, status=%s%s)",
                    v["place_id"], v["name"], resp.status,
                    f", {resp.error}" if resp.error else "",
                )
                db.set_stage(conn, v["place_id"], "eliminated", "website_dead")
            else:
                funnel["website_dead_first_strike"] += 1
                logger.info(
                    "%s | %s | website_dead_once -> needs_review (status=%s%s)",
                    v["place_id"], v["name"], resp.status,
                    f", {resp.error}" if resp.error else "",
                )
                db.set_stage(conn, v["place_id"], "needs_review", "website_dead_once")
            continue

        title, og_name = extract_identity_fields(resp.text)
        score = identity_score(v["name"], resp.final_url, title, og_name)
        verdict = identity_verdict(score)
        if verdict == "pass":
            funnel["passed"] += 1
            db.set_stage(conn, v["place_id"], "2_filtered_ok")
        elif verdict == "mismatch":
            funnel["website_identity_mismatch"] += 1
            logger.info(
                "%s | %s | website_identity_mismatch (score=%.2f, final=%s, title=%r)",
                v["place_id"], v["name"], score, resp.final_url, title[:60],
            )
            db.set_stage(conn, v["place_id"], "eliminated", "website_identity_mismatch")
        else:
            funnel["needs_review"] += 1
            logger.info(
                "%s | %s | needs_review (score=%.2f, final=%s, title=%r)",
                v["place_id"], v["name"], score, resp.final_url, title[:60],
            )
            db.set_stage(conn, v["place_id"], "needs_review")

    return funnel


def print_funnel(funnel: Counter, dry_run: bool) -> None:
    order = [
        "not_operational", "rating_below_4", "too_few_reviews",
        "price_level_high", "no_website", "website_dead",
        "website_identity_mismatch",
    ]
    print(f"\nfunnel — in (1_geo_ok): {funnel['in_1_geo_ok']}"
          + (f" + retry queue: {funnel['in_website_retry_queue']}"
             if funnel["in_website_retry_queue"] else ""))
    for reason in order:
        if funnel[reason]:
            print(f"  eliminated {reason:<28} {funnel[reason]:>5}")
    if funnel["website_dead_first_strike"]:
        print(f"  {'website_dead_once -> needs_review':<39} "
              f"{funnel['website_dead_first_strike']:>5}")
    if funnel["needs_review"]:
        print(f"  {'needs_review (identity)':<39} {funnel['needs_review']:>5}")
    if dry_run:
        print(f"  would check website for {funnel['would_check_website']} rows (dry run)")
    print(f"out (2_filtered_ok): {funnel['passed']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2 cheap filter gate")
    parser.add_argument("--limit", type=int, default=None, help="max rows to process")
    parser.add_argument("--dry-run", action="store_true",
                        help="offline checks only; no network, no DB writes")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = db.connect()
    db.init_db(conn)
    funnel = run(conn, limit=args.limit, dry_run=args.dry_run)
    print_funnel(funnel, args.dry_run)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
