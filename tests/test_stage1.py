"""Stage 1 pure-logic tests — no network, fixture-driven."""

import json
import math
from pathlib import Path

from src import stage1_search
from src.stage1_search import (
    PRICE_LEVEL_MAP,
    in_zone,
    is_blocked_type,
    load_cells,
    normalize_place,
    sub_circles,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "places_response.json").read_text()
)

RIVER_NORTH = {"lat": 41.8924, "lng": -87.6341}


# --- geo circle check ---

def test_in_zone_center_and_far_point():
    assert in_zone(41.8924, -87.6341, RIVER_NORTH, 1000)
    # Wicker Park is ~4km away — outside a 1km circle
    assert not in_zone(41.9088, -87.6773, RIVER_NORTH, 1000)


def test_in_zone_boundary():
    # ~900m north of center: inside 1000m, outside 800m
    lat_900m_north = 41.8924 + 900 / 111_320.0
    assert in_zone(lat_900m_north, -87.6341, RIVER_NORTH, 1000)
    assert not in_zone(lat_900m_north, -87.6341, RIVER_NORTH, 800)


def test_in_zone_rect_corner_is_outside_circle():
    # A point at the bounding-rect corner (radius in BOTH lat and lng) is
    # sqrt(2)*radius from center — must be killed by the circle check.
    dlat = 1000 / 111_320.0
    dlng = 1000 / (111_320.0 * math.cos(math.radians(41.8924)))
    assert not in_zone(41.8924 + dlat, -87.6341 + dlng, RIVER_NORTH, 1000)


# --- type blocklist ---

def test_blocklist_kills_lodging_casino_liquor_store():
    assert is_blocked_type(["bar", "lodging"], "bar")
    assert is_blocked_type(["casino"], "casino")
    assert is_blocked_type(["liquor_store", "store"], "liquor_store")


def test_blocklist_primary_night_club_always_killed():
    # 2026-07-08: unconditional — the old bar/restaurant-hybrid exemption is gone
    assert is_blocked_type(["night_club", "point_of_interest"], "night_club")
    assert is_blocked_type(["night_club", "bar"], "night_club")
    assert is_blocked_type(["night_club", "restaurant"], "night_club")


def test_blocklist_corporate_fit_primaries_killed():
    for primary in ("cafe", "coffee_shop", "breakfast_restaurant",
                    "barber_shop", "miniature_golf_course", "movie_theater",
                    "sports_complex", "bowling_alley", "amusement_center", "karaoke"):
        assert is_blocked_type([primary, "establishment"], primary), primary


def test_blocklist_secondary_tags_never_kill():
    # The Publican pattern: great venues carry brunch/cafe as SECONDARY tags
    assert not is_blocked_type(["restaurant", "brunch_restaurant"], "american_restaurant")
    assert not is_blocked_type(["bar", "cafe", "coffee_shop"], "wine_bar")
    assert not is_blocked_type(["restaurant", "breakfast_restaurant"], "steak_house")
    assert not is_blocked_type(["bar", "night_club"], "bar")


def test_blocklist_passes_normal_venues():
    assert not is_blocked_type(["bar", "point_of_interest"], "bar")
    assert not is_blocked_type(["restaurant"], "restaurant")
    assert not is_blocked_type([], None)


def test_blocklist_is_config_driven():
    cfg = json.loads(
        (stage1_search.CONFIG_DIR / "blocked_types.json").read_text()
    )
    assert stage1_search.BLOCKED_TYPES == set(cfg["blocked_types"])
    assert stage1_search.BLOCKED_PRIMARY_TYPES == set(cfg["blocked_primary_types"])


# --- normalization against the real captured fixture ---

def test_normalize_all_fixture_places():
    places = [p for page in FIXTURE for p in page.get("places", [])]
    assert len(places) == 60
    for place in places:
        norm = normalize_place(place, "river_north")
        assert norm["place_id"]
        assert norm["name"]
        assert norm["zone_id"] == "river_north"
        assert norm["price_level"] in (None, 0, 1, 2, 3, 4)
        assert isinstance(norm["types"], list)


def test_normalize_field_mapping():
    place = FIXTURE[0]["places"][0]
    norm = normalize_place(place, "river_north")
    assert norm["place_id"] == place["id"]
    assert norm["name"] == place["displayName"]["text"]
    assert norm["lat"] == place["location"]["latitude"]
    assert norm["rating"] == place.get("rating")
    assert norm["user_rating_count"] == place.get("userRatingCount")


def test_price_level_mapping():
    assert PRICE_LEVEL_MAP["PRICE_LEVEL_MODERATE"] == 2
    assert PRICE_LEVEL_MAP["PRICE_LEVEL_VERY_EXPENSIVE"] == 4
    assert PRICE_LEVEL_MAP.get("PRICE_LEVEL_UNSPECIFIED") is None
    assert PRICE_LEVEL_MAP.get(None) is None


# --- 60-cap subdivision geometry ---

def test_sub_circles_geometry():
    subs = sub_circles(RIVER_NORTH, 1000)
    assert len(subs) == 4
    for center, radius in subs:
        assert radius == 500
        # each center is offset 500m in |lat| and 500m in |lng|
        assert abs(abs(center["lat"] - RIVER_NORTH["lat"]) * 111_320.0 - 500) < 1
    # all four quadrant sign combinations present
    signs = {
        (
            1 if c["lat"] > RIVER_NORTH["lat"] else -1,
            1 if c["lng"] > RIVER_NORTH["lng"] else -1,
        )
        for c, _ in subs
    }
    assert signs == {(1, 1), (1, -1), (-1, 1), (-1, -1)}


# --- config cells ---

def test_load_cells_builds_50_with_zone_substituted():
    cells = load_cells()
    assert len(cells) == 50
    cell = cells["river_north__cocktail_bar"]
    assert cell["queries"][0] == "cocktail bar River North Chicago"
    assert all("{zone}" not in q for c in cells.values() for q in c["queries"])
