"""Cached, rate-limited HTTP fetch layer — Work order 4 (Milestone 3).

get(url) -> CachedResponse. Raw responses cache to cache/{sha256(url)}.json
(status, final_url, headers subset, text); re-running any stage hits the
cache, never the live web. Per-domain minimum interval of 1.0s; full
browser-like header set (restaurant hosting platforms 403 bare-UA clients);
follows redirects; 10s timeout; one retry after 3s on 403/429/timeout.

Transport failures (DNS, TLS, timeouts) are NOT cached — a transient outage
must not poison re-runs. HTTP responses of any status ARE cached.
Playwright fallback arrives in Milestone 4.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
# Full browser-like header set: restaurant hosting platforms (Popmenu,
# Owner.com, Cloudflare-fronted sites) 403 bare-UA requests.
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
MIN_INTERVAL_S = 1.0
DEFAULT_TIMEOUT_S = 10.0
RETRY_AFTER_S = 3.0
RETRY_STATUSES = (403, 429)

_last_hit: dict[str, float] = {}


@dataclass
class CachedResponse:
    url: str
    status: int          # 0 = transport failure (see error)
    final_url: str
    headers: dict[str, str]
    text: str
    error: Optional[str] = None
    from_cache: bool = field(default=False)


def _cache_path(url: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha256(url.encode()).hexdigest()}.json"


def _throttle(host: str) -> None:
    now = time.monotonic()
    last = _last_hit.get(host)
    if last is not None and now - last < MIN_INTERVAL_S:
        time.sleep(MIN_INTERVAL_S - (now - last))
    _last_hit[host] = time.monotonic()


def _do_get(url: str, timeout: float) -> httpx.Response:
    return httpx.get(url, follow_redirects=True, timeout=timeout, headers=BASE_HEADERS)


def get(url: str, timeout: float = DEFAULT_TIMEOUT_S) -> CachedResponse:
    path = _cache_path(url)
    if path.exists():
        data = json.loads(path.read_text())
        return CachedResponse(**data, from_cache=True)

    host = urlparse(url).netloc
    _throttle(host)
    resp: Optional[httpx.Response] = None
    try:
        resp = _do_get(url, timeout)
    except httpx.TimeoutException as exc:
        logger.info("fetch timeout | %s | %s — retrying in %.0fs", url, exc, RETRY_AFTER_S)
    except httpx.HTTPError as exc:
        logger.info("fetch transport error | %s | %s", url, exc)
        return CachedResponse(
            url=url, status=0, final_url=url, headers={}, text="",
            error=f"{type(exc).__name__}: {exc}",
        )

    # one retry after 3s on 403/429/timeout
    if resp is None or resp.status_code in RETRY_STATUSES:
        if resp is not None:
            logger.info(
                "fetch got %d | %s — retrying in %.0fs", resp.status_code, url, RETRY_AFTER_S
            )
        time.sleep(RETRY_AFTER_S)
        _throttle(host)
        try:
            resp = _do_get(url, timeout)
        except httpx.HTTPError as exc:
            logger.info("fetch retry failed | %s | %s", url, exc)
            return CachedResponse(
                url=url, status=0, final_url=url, headers={}, text="",
                error=f"{type(exc).__name__}: {exc}",
            )

    cached = CachedResponse(
        url=url,
        status=resp.status_code,
        final_url=str(resp.url),
        headers={"content-type": resp.headers.get("content-type", "")},
        text=resp.text,
    )
    # Never cache bot-challenge statuses: a cached 403 would let a later run
    # "confirm" a dead website without ever re-checking the live site.
    if resp.status_code in RETRY_STATUSES:
        return cached
    CACHE_DIR.mkdir(exist_ok=True)
    payload = asdict(cached)
    payload.pop("from_cache")
    path.write_text(json.dumps(payload))
    return cached
