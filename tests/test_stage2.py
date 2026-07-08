"""Stage 2 filter tests — offline checks against hand-built records (no
network), identity scoring, verdict thresholds, and the website_dead
two-strike policy with Playwright escalation."""

import pytest

from src import db, stage2_filter
from src.fetch import CachedResponse
from src.stage2_filter import (
    extract_identity_fields,
    identity_score,
    identity_verdict,
    name_tokens,
    offline_check,
)


def record(**overrides):
    base = {
        "business_status": "OPERATIONAL",
        "rating": 4.5,
        "user_rating_count": 500,
        "price_level": 2,
        "website_uri": "https://example-venue.com",
        "types": ["bar"],
        "primary_type": "bar",
    }
    base.update(overrides)
    return base


# --- hand-built records, offline checks 1-4, right reason codes ---

def test_blocked_type_primary_category():
    assert offline_check(record(primary_type="night_club",
                                types=["night_club", "bar"])) == "blocked_type"
    assert offline_check(record(primary_type="cafe", types=["cafe"])) == "blocked_type"
    assert offline_check(record(types=["bar", "lodging"])) == "blocked_type"


def test_blocked_type_reads_types_json_from_db_rows():
    r = record()
    del r["types"]
    r["types_json"] = '["bar", "lodging"]'
    assert offline_check(r) == "blocked_type"
    r["types_json"] = '["bar"]'
    assert offline_check(r) is None


def test_secondary_tags_do_not_block():
    assert offline_check(record(primary_type="american_restaurant",
                                types=["restaurant", "brunch_restaurant"])) is None


def test_not_operational():
    assert offline_check(record(business_status="CLOSED_PERMANENTLY")) == "not_operational"


def test_price_level_high():
    assert offline_check(record(price_level=4)) == "price_level_high"


def test_no_website():
    assert offline_check(record(website_uri=None)) == "no_website"


def test_all_offline_checks_pass():
    assert offline_check(record()) is None


# --- ordering and edge cases ---

def test_first_failure_wins():
    # fails everything — must report check 1's reason (blocked_type first)
    r = record(primary_type="night_club", types=["night_club"],
               business_status="CLOSED_TEMPORARILY", rating=3.0,
               user_rating_count=5, price_level=4, website_uri=None)
    assert offline_check(r) == "blocked_type"
    # fails 2..4 — operational before price before website
    r = record(business_status="CLOSED_TEMPORARILY", price_level=4, website_uri=None)
    assert offline_check(r) == "not_operational"
    assert offline_check(record(price_level=4, website_uri=None)) == "price_level_high"


def test_rating_and_review_count_are_not_gated():
    # gate removed 2026-07-08: quality is Stage 4's job, never a kill here —
    # rating_below_4 / too_few_reviews must no longer be producible
    assert offline_check(record(rating=1.0, user_rating_count=1)) is None
    assert offline_check(record(rating=None, user_rating_count=None)) is None


def test_null_price_level_passes():
    assert offline_check(record(price_level=None)) is None


def test_boundary_values_pass():
    assert offline_check(record(price_level=3)) is None


# --- identity scoring ---

def test_identity_exact_domain_match():
    score = identity_score(
        "The Violet Hour", "https://theviolethour.com/",
        "The Violet Hour | Cocktails in Wicker Park", "",
    )
    assert score >= 0.6
    assert identity_verdict(score) == "pass"


def test_identity_squashed_domain_and_punctuation():
    # apostrophes/ampersands must not break matching
    score = identity_score("Gus' Sip & Dip", "https://gussipanddip.com/", "", "")
    assert score >= 0.6


def test_identity_og_site_name_counts():
    score = identity_score(
        "Machine Cocktail Bar", "https://linktr.ee/something",
        "Home", "Machine: Engineered Dining & Cocktails",
    )
    assert score >= 0.6


def test_identity_mismatch():
    score = identity_score(
        "The Violet Hour", "https://squarespace.com/", "Coming Soon — Squarespace", "",
    )
    assert score <= 0.2
    assert identity_verdict(score) == "mismatch"


def test_identity_middle_band_goes_to_review():
    # 2 of 4 distinctive tokens present -> 0.5 -> review band
    score = identity_score(
        "Three Dots and a Dash Tiki", "https://unrelated-hospitality.com/",
        "Three Dots | Reservations", "",
    )
    assert 0.2 < score < 0.6
    assert identity_verdict(score) == "review"


def test_name_tokens_generic_fallback():
    # a name made entirely of generic tokens falls back to raw tokens
    assert name_tokens("The Bar Chicago") == {"the", "bar", "chicago"}
    assert name_tokens("The Violet Hour") == {"violet", "hour"}


def test_verdict_thresholds_exact():
    assert identity_verdict(0.6) == "pass"
    assert identity_verdict(0.2) == "mismatch"
    assert identity_verdict(0.21) == "review"
    assert identity_verdict(0.59) == "review"


# --- website_dead two-strike policy + Playwright escalation
#     (fetch monkeypatched, no network) ---

@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_db(conn)
    db.upsert_venue(conn, {
        "place_id": "ChIJtwostrike",
        "name": "The Violet Hour",
        "rating": 4.6, "user_rating_count": 2100, "price_level": 3,
        "business_status": "OPERATIONAL",
        "website_uri": "https://theviolethour.com",
        "types": ["bar"], "primary_type": "bar",
        "found_by": {"cell_id": "c", "query": "q", "rank": 1, "run_id": "r"},
    })
    db.set_stage(conn, "ChIJtwostrike", "1_geo_ok")
    yield conn
    conn.close()


def _resp(status, url="https://theviolethour.com", text=""):
    return CachedResponse(url=url, status=status, final_url=url, headers={}, text=text)


def _patch_fetch(monkeypatch, get_status, rendered_status=None, rendered_text=""):
    """Patch httpx and Playwright paths. rendered_status=None asserts the
    escalation is never attempted."""
    rendered_calls = []
    monkeypatch.setattr(stage2_filter.fetch, "get",
                        lambda url, timeout=10.0: _resp(get_status))
    def get_rendered(url, timeout=30.0):
        rendered_calls.append(url)
        assert rendered_status is not None, "escalated when it must not"
        return _resp(rendered_status, text=rendered_text)
    monkeypatch.setattr(stage2_filter.fetch, "get_rendered", get_rendered)
    return rendered_calls


def test_website_dead_two_strike_only_after_rendered_also_fails(conn, monkeypatch):
    rendered_calls = _patch_fetch(monkeypatch, 403, rendered_status=403)
    # strike 1: httpx 403 AND rendered 403 -> needs_review with marker
    funnel = stage2_filter.run(conn)
    assert funnel["website_dead_first_strike"] == 1
    assert rendered_calls, "403 must escalate to the rendered fetch"
    row = db.get_venue(conn, "ChIJtwostrike")
    assert row["stage"] == "needs_review"
    assert row["eliminated_reason"] == "website_dead_once"
    # strike 2: retry queue re-checked, both paths still failing -> eliminated
    funnel = stage2_filter.run(conn)
    assert funnel["in_website_retry_queue"] == 1
    assert funnel["website_dead"] == 1
    assert len(rendered_calls) == 2
    row = db.get_venue(conn, "ChIJtwostrike")
    assert row["stage"] == "eliminated"
    assert row["eliminated_reason"] == "website_dead"


def test_rendered_200_rescues_and_feeds_identity(conn, monkeypatch):
    # httpx 403, rendered 200 with matching title -> no strike, straight pass
    _patch_fetch(monkeypatch, 403, rendered_status=200,
                 rendered_text="<title>The Violet Hour</title>")
    funnel = stage2_filter.run(conn)
    assert funnel["website_rescued_by_render"] == 1
    assert funnel["website_dead_first_strike"] == 0
    assert funnel["passed"] == 1
    row = db.get_venue(conn, "ChIJtwostrike")
    assert row["stage"] == "2_filtered_ok"
    assert row["eliminated_reason"] is None


def test_rendered_200_wrong_identity_still_reaches_check_6(conn, monkeypatch):
    # escalation rescues liveness but identity still judges the rendered
    # html + final url (here: parked on an unrelated domain)
    monkeypatch.setattr(stage2_filter.fetch, "get",
                        lambda url, timeout=10.0: _resp(403))
    monkeypatch.setattr(
        stage2_filter.fetch, "get_rendered",
        lambda url, timeout=30.0: _resp(200, url="https://parked-domains.example/",
                                        text="<title>Coming Soon</title>"),
    )
    funnel = stage2_filter.run(conn)
    assert funnel["website_rescued_by_render"] == 1
    assert db.get_venue(conn, "ChIJtwostrike")["eliminated_reason"] \
        == "website_identity_mismatch"


def test_rescue_from_retry_queue_clears_marker(conn, monkeypatch):
    # strike one first (both paths fail) ...
    _patch_fetch(monkeypatch, 403, rendered_status=403)
    stage2_filter.run(conn)
    assert db.get_venue(conn, "ChIJtwostrike")["eliminated_reason"] == "website_dead_once"
    # ... next run the rendered fetch gets through -> promoted, marker cleared
    _patch_fetch(monkeypatch, 403, rendered_status=200,
                 rendered_text="<title>The Violet Hour</title>")
    funnel = stage2_filter.run(conn)
    assert funnel["passed"] == 1
    row = db.get_venue(conn, "ChIJtwostrike")
    assert row["stage"] == "2_filtered_ok"
    assert row["eliminated_reason"] is None


def test_robots_disallowed_not_escalated(conn, monkeypatch):
    _patch_fetch(monkeypatch, -1, rendered_status=None)  # rendered call = failure
    funnel = stage2_filter.run(conn)
    assert funnel["website_dead_first_strike"] == 1
    assert db.get_venue(conn, "ChIJtwostrike")["eliminated_reason"] == "website_dead_once"


def test_plain_404_not_escalated(conn, monkeypatch):
    _patch_fetch(monkeypatch, 404, rendered_status=None)
    funnel = stage2_filter.run(conn)
    assert funnel["website_dead_first_strike"] == 1


def test_timeout_and_5xx_do_escalate():
    assert stage2_filter._should_escalate(0)      # timeout / transport
    assert stage2_filter._should_escalate(403)
    assert stage2_filter._should_escalate(429)
    assert stage2_filter._should_escalate(503)
    assert not stage2_filter._should_escalate(-1)  # robots_disallowed
    assert not stage2_filter._should_escalate(404)
    assert not stage2_filter._should_escalate(410)


def test_website_recovers_after_first_strike(conn, monkeypatch):
    _patch_fetch(monkeypatch, 403, rendered_status=403)
    stage2_filter.run(conn)
    assert db.get_venue(conn, "ChIJtwostrike")["stage"] == "needs_review"
    # site comes back with a matching title -> full pass, marker cleared
    monkeypatch.setattr(
        stage2_filter.fetch, "get",
        lambda url, timeout=10.0: _resp(200, text="<title>The Violet Hour</title>"),
    )
    funnel = stage2_filter.run(conn)
    assert funnel["passed"] == 1
    row = db.get_venue(conn, "ChIJtwostrike")
    assert row["stage"] == "2_filtered_ok"
    assert row["eliminated_reason"] is None


def test_dry_run_does_not_touch_retry_queue(conn, monkeypatch):
    _patch_fetch(monkeypatch, 403, rendered_status=403)
    stage2_filter.run(conn)
    funnel = stage2_filter.run(conn, dry_run=True)
    assert funnel["in_website_retry_queue"] == 1
    assert funnel["website_dead"] == 0
    assert db.get_venue(conn, "ChIJtwostrike")["stage"] == "needs_review"


# --- retroactive blocked-types sweep (no network) ---

def _seed(conn, place_id, name, primary_type, types, stage):
    db.upsert_venue(conn, {
        "place_id": place_id, "name": name,
        "rating": 4.2, "user_rating_count": 300, "price_level": 2,
        "business_status": "OPERATIONAL", "website_uri": f"https://{place_id}.test",
        "types": types, "primary_type": primary_type,
        "found_by": {"cell_id": "c", "query": "q", "rank": 1, "run_id": "r"},
    })
    db.set_stage(conn, place_id, stage)


def test_sweep_blocked_types(conn):
    _seed(conn, "ChIJclub", "Bass Cavern", "night_club", ["night_club", "bar"],
          "3_enriched")
    _seed(conn, "ChIJcafe", "Bean There", "coffee_shop", ["coffee_shop", "cafe"],
          "needs_review")
    _seed(conn, "ChIJgood", "The Publican-alike", "american_restaurant",
          ["restaurant", "brunch_restaurant"], "2_filtered_ok")
    _seed(conn, "ChIJdead", "Already Dead Club", "night_club", ["night_club"],
          "2_filtered_ok")
    db.set_stage(conn, "ChIJdead", "eliminated", "website_dead")

    # dry run: reports, writes nothing
    funnel = stage2_filter.sweep_blocked_types(conn, dry_run=True)
    assert funnel["blocked_type"] == 2
    assert db.get_venue(conn, "ChIJclub")["stage"] == "3_enriched"

    # live: blocked rows eliminated with reason; clean + already-dead untouched
    funnel = stage2_filter.sweep_blocked_types(conn)
    assert funnel["blocked_type"] == 2
    for pid in ("ChIJclub", "ChIJcafe"):
        row = db.get_venue(conn, pid)
        assert row["stage"] == "eliminated"
        assert row["eliminated_reason"] == "blocked_type"
    assert db.get_venue(conn, "ChIJgood")["stage"] == "2_filtered_ok"
    assert db.get_venue(conn, "ChIJdead")["eliminated_reason"] == "website_dead"

    # idempotent: second run finds nothing new
    funnel = stage2_filter.sweep_blocked_types(conn)
    assert funnel["blocked_type"] == 0


# --- html field extraction ---

def test_extract_identity_fields():
    html = """<html><head><title> The Violet Hour </title>
    <meta property="og:site_name" content="The Violet Hour"/></head></html>"""
    title, og = extract_identity_fields(html)
    assert title == "The Violet Hour"
    assert og == "The Violet Hour"


def test_extract_identity_fields_empty_html():
    assert extract_identity_fields("") == ("", "")
    assert extract_identity_fields("<html><body>hi</body></html>") == ("", "")
