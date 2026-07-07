"""Cached, rate-limited HTTP fetch layer — Work orders 4 + M4.

get(url) -> CachedResponse. Raw responses cache to cache/{sha256(url)}.json
(status, final_url, headers subset, text, and base64 body for binary content
types); re-running any stage hits the cache, never the live web. Per-domain
minimum interval of 1.0s; full browser-like header set; follows redirects;
10s timeout; one retry after 3s on 403/429/timeout. Bot-challenge responses
(403/429) and transport failures are never cached.

robots.txt is respected (fail-open on errors/4xx): a disallowed URL returns
status=-1 with error="robots_disallowed" and is never fetched.

get_rendered(url) is the Playwright fallback for JA3-blocked sites and
JS-shell pages (Popmenu/Owner.com/Cloudflare 403 the Python TLS stack — see
NOTES.md). It caches under a separate key ("playwright:" + url). Playwright
is imported lazily; if not installed, a status=0 response explains that.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import urllib.robotparser
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

TEXT_CONTENT_MARKERS = ("text/", "html", "xml", "json", "javascript")

_last_hit: dict[str, float] = {}
# host -> RobotFileParser (None = no usable robots.txt, allow all)
_robots_mem: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}


@dataclass
class CachedResponse:
    url: str
    status: int          # 0 = transport failure; -1 = robots_disallowed
    final_url: str
    headers: dict[str, str]
    text: str
    content_b64: Optional[str] = None  # body for binary content types (pdf/images)
    error: Optional[str] = None
    from_cache: bool = field(default=False)

    @property
    def content(self) -> bytes:
        return base64.b64decode(self.content_b64) if self.content_b64 else self.text.encode()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def _read_cache(key: str) -> Optional[CachedResponse]:
    path = _cache_path(key)
    if not path.exists():
        return None
    return CachedResponse(**json.loads(path.read_text()), from_cache=True)


def _write_cache(key: str, resp: CachedResponse) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    payload = asdict(resp)
    payload.pop("from_cache")
    _cache_path(key).write_text(json.dumps(payload))


def _throttle(host: str) -> None:
    now = time.monotonic()
    last = _last_hit.get(host)
    if last is not None and now - last < MIN_INTERVAL_S:
        time.sleep(MIN_INTERVAL_S - (now - last))
    _last_hit[host] = time.monotonic()


def _do_get(url: str, timeout: float) -> httpx.Response:
    return httpx.get(url, follow_redirects=True, timeout=timeout, headers=BASE_HEADERS)


def _is_text_content(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return any(marker in ct for marker in TEXT_CONTENT_MARKERS) or ct == ""


def _robots_allowed(url: str) -> bool:
    """robots.txt check, fail-open. Parsers memoized per host per process;
    200 robots bodies also land in the persistent cache."""
    parsed = urlparse(url)
    host = parsed.netloc
    if host not in _robots_mem:
        robots_url = f"{parsed.scheme}://{host}/robots.txt"
        cached = _read_cache(robots_url)
        if cached is not None:
            body = cached.text if cached.status == 200 else None
        else:
            _throttle(host)
            try:
                resp = _do_get(robots_url, DEFAULT_TIMEOUT_S)
            except httpx.HTTPError as exc:
                logger.info("robots.txt fetch failed (fail-open) | %s | %s", robots_url, exc)
                resp = None
            body = resp.text if resp is not None and resp.status_code == 200 else None
            if resp is not None and resp.status_code == 200:
                _write_cache(robots_url, CachedResponse(
                    url=robots_url, status=200, final_url=str(resp.url),
                    headers={"content-type": resp.headers.get("content-type", "")},
                    text=resp.text,
                ))
        if body is None:
            _robots_mem[host] = None
        else:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(body.splitlines())
            _robots_mem[host] = rp
    rp = _robots_mem[host]
    return rp is None or rp.can_fetch(USER_AGENT, url)


def _build_response(url: str, resp: httpx.Response) -> CachedResponse:
    content_type = resp.headers.get("content-type", "")
    if _is_text_content(content_type):
        text, blob = resp.text, None
    else:
        text, blob = "", base64.standard_b64encode(resp.content).decode()
    return CachedResponse(
        url=url, status=resp.status_code, final_url=str(resp.url),
        headers={"content-type": content_type}, text=text, content_b64=blob,
    )


def get(url: str, timeout: float = DEFAULT_TIMEOUT_S) -> CachedResponse:
    cached = _read_cache(url)
    if cached is not None:
        return cached

    if not _robots_allowed(url):
        logger.info("robots_disallowed | %s", url)
        return CachedResponse(url=url, status=-1, final_url=url, headers={}, text="",
                              error="robots_disallowed")

    host = urlparse(url).netloc
    _throttle(host)
    resp: Optional[httpx.Response] = None
    try:
        resp = _do_get(url, timeout)
    except httpx.TimeoutException as exc:
        logger.info("fetch timeout | %s | %s — retrying in %.0fs", url, exc, RETRY_AFTER_S)
    except httpx.HTTPError as exc:
        logger.info("fetch transport error | %s | %s", url, exc)
        return CachedResponse(url=url, status=0, final_url=url, headers={}, text="",
                              error=f"{type(exc).__name__}: {exc}")

    # one retry after 3s on 403/429/timeout
    if resp is None or resp.status_code in RETRY_STATUSES:
        if resp is not None:
            logger.info("fetch got %d | %s — retrying in %.0fs", resp.status_code, url, RETRY_AFTER_S)
        time.sleep(RETRY_AFTER_S)
        _throttle(host)
        try:
            resp = _do_get(url, timeout)
        except httpx.HTTPError as exc:
            logger.info("fetch retry failed | %s | %s", url, exc)
            return CachedResponse(url=url, status=0, final_url=url, headers={}, text="",
                                  error=f"{type(exc).__name__}: {exc}")

    result = _build_response(url, resp)
    # Never cache bot-challenge statuses: a cached 403 would let a later run
    # "confirm" a dead website without ever re-checking the live site.
    if resp.status_code not in RETRY_STATUSES:
        _write_cache(url, result)
    return result


# --- Playwright fallback (M4) ---

RENDER_TIMEOUT_S = 30.0
CHALLENGE_MARKER = "Just a moment"


def get_rendered(url: str, timeout: float = RENDER_TIMEOUT_S) -> CachedResponse:
    """Fetch a page with a real headless browser (Chromium). For JA3-blocked
    hosts and JS-shell pages. Cached separately from raw fetches."""
    key = f"playwright:{url}"
    cached = _read_cache(key)
    if cached is not None:
        return cached

    if not _robots_allowed(url):
        logger.info("robots_disallowed (rendered) | %s", url)
        return CachedResponse(url=url, status=-1, final_url=url, headers={}, text="",
                              error="robots_disallowed")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return CachedResponse(url=url, status=0, final_url=url, headers={}, text="",
                              error="playwright_not_installed")

    _throttle(urlparse(url).netloc)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=USER_AGENT,  # headless shell self-identifies otherwise
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            # Cloudflare JS challenges resolve themselves in a real browser —
            # give them a few beats before reading the DOM.
            for _ in range(6):
                if CHALLENGE_MARKER not in (page.title() or ""):
                    break
                page.wait_for_timeout(2500)
            page.wait_for_timeout(1000)  # let late JS settle
            status = resp.status if resp is not None else 0
            # the challenge navigates in-place on success; re-read the response
            if CHALLENGE_MARKER not in (page.title() or "") and status in RETRY_STATUSES:
                status = 200
            html = page.content()
            final_url = page.url
            browser.close()
    except Exception as exc:  # playwright raises its own error types
        logger.info("rendered fetch failed | %s | %s", url, exc)
        return CachedResponse(url=url, status=0, final_url=url, headers={}, text="",
                              error=f"{type(exc).__name__}: {exc}")

    result = CachedResponse(
        url=url, status=status, final_url=final_url,
        headers={"content-type": "text/html"}, text=html,
    )
    if status == 200 and CHALLENGE_MARKER not in html[:2000]:
        _write_cache(key, result)
    else:
        logger.info("rendered fetch not cached | %s | status=%s", url, status)
    return result
