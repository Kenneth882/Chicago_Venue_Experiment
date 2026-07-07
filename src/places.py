"""Places API (New) Text Search client — Work order 2 (Milestone 1).

POST places:searchText with the frozen field mask and a locationRestriction.
NOTE — deviation from SPEC.md wording, forced by the API: searchText rejects
`circle` inside locationRestriction (only Nearby Search supports that; Text
Search allows circle only in locationBias, which is soft). We send the
circle's bounding RECTANGLE as the hard restriction; Stage 1's shapely
point-in-circle check trims the corners, so the effective boundary is still
the spec'd circle, decided in code. Pagination is hard-capped at 3 pages
(60 results) in code. tenacity
backs off exponentially on 429/5xx (max 5 tries); an INVALID_REQUEST on a
fresh pageToken gets one retry after a 2s sleep (tokens are briefly stale).

Dry-run lives at the caller level: pass dry_run=True and search_text logs the
request it would send and returns [] without touching the network. The module
counter `request_count` tracks real HTTP POSTs so callers can assert that
--dry-run made zero calls (M2 acceptance #3).
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Exactly the mask from CLAUDE.md, plus nextPageToken for pagination.
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.rating",
        "places.userRatingCount",
        "places.priceLevel",
        "places.businessStatus",
        "places.websiteUri",
        "places.types",
        "places.primaryType",
        "nextPageToken",
    ]
)

MAX_PAGES = 3  # hard cap: 3 pages x 20 = 60 results per query

# Real HTTP POSTs made by this process. Never incremented on dry_run.
request_count = 0


def _api_key() -> str:
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not set (load .env first)")
    return key


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _post(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    global request_count
    request_count += 1
    resp = httpx.post(
        SEARCH_URL,
        json=payload,
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": FIELD_MASK,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _post_page(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    """One page fetch, with the fresh-pageToken INVALID_REQUEST quirk handled:
    a nextPageToken can take a moment to become valid — sleep 2s, retry once."""
    try:
        return _post(payload, api_key)
    except httpx.HTTPStatusError as exc:
        if (
            "pageToken" in payload
            and exc.response.status_code == 400
            and "INVALID_REQUEST" in exc.response.text
        ):
            logger.info("INVALID_REQUEST on fresh pageToken; sleeping 2s and retrying once")
            time.sleep(2)
            return _post(payload, api_key)
        raise


def _bounding_rect(center: dict[str, float], radius_m: float) -> dict[str, Any]:
    """Axis-aligned bounding box of a circle, for searchText's
    locationRestriction (which accepts rectangle only, not circle)."""
    dlat = radius_m / 111_320.0
    dlng = radius_m / (111_320.0 * math.cos(math.radians(center["lat"])))
    return {
        "rectangle": {
            "low": {"latitude": center["lat"] - dlat, "longitude": center["lng"] - dlng},
            "high": {"latitude": center["lat"] + dlat, "longitude": center["lng"] + dlng},
        }
    }


def search_text(
    query: str,
    center: dict[str, float],
    radius_m: float,
    included_type: Optional[str] = None,
    *,
    dry_run: bool = False,
    page_sink: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Run one Text Search query, paginating up to MAX_PAGES. Returns raw
    place dicts (normalization is Stage 1's job).

    center is a zones.json-style dict: {"lat": ..., "lng": ...}.
    page_sink, if given, collects each raw response page (fixture capture).
    """
    body: dict[str, Any] = {
        "textQuery": query,
        "locationRestriction": _bounding_rect(center, float(radius_m)),
    }
    if included_type:
        body["includedType"] = included_type

    if dry_run:
        logger.info("DRY RUN — would POST %s body=%s (up to %d pages)", SEARCH_URL, body, MAX_PAGES)
        return []

    api_key = _api_key()
    results: list[dict[str, Any]] = []
    page_token: Optional[str] = None

    for page in range(1, MAX_PAGES + 1):
        payload = dict(body)
        if page_token:
            payload["pageToken"] = page_token
        data = _post_page(payload, api_key)
        if page_sink is not None:
            page_sink.append(data)
        places = data.get("places", [])
        results.extend(places)
        page_token = data.get("nextPageToken")
        logger.info(
            "query=%r page=%d results=%d next_token=%s",
            query, page, len(places), "yes" if page_token else "no",
        )
        if not page_token:
            break

    return results
