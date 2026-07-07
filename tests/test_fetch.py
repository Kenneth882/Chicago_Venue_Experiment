"""fetch.py cache, retry, robots, and binary-body behavior — httpx layer
monkeypatched, no network."""

import base64

import httpx
import pytest

from src import fetch


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    fetch._last_hit.clear()
    fetch._robots_mem.clear()
    yield tmp_path


def _fake(status, text="body", headers=None):
    def do_get(url, timeout):
        return httpx.Response(
            status, text=text, headers=headers or {"content-type": "text/html"},
            request=httpx.Request("GET", url),
        )
    return do_get


def test_403_retried_once_and_not_cached(isolated_cache, monkeypatch):
    calls = []
    def counting(url, timeout):
        calls.append(url)
        return httpx.Response(403, text="Just a moment...", request=httpx.Request("GET", url))
    monkeypatch.setattr(fetch, "_do_get", counting)
    r = fetch.get("https://blocked.test/")
    assert r.status == 403
    # robots.txt probe (403 -> fail-open, no retry), then the URL + one retry
    assert calls[0] == "https://blocked.test/robots.txt"
    assert calls[1:] == ["https://blocked.test/", "https://blocked.test/"]
    assert list(isolated_cache.glob("*.json")) == []  # challenge NOT cached


def test_blocked_site_rechecks_live_next_time(isolated_cache, monkeypatch):
    monkeypatch.setattr(fetch, "_do_get", _fake(403))
    assert fetch.get("https://blocked.test/").status == 403
    # site unblocks: next get must hit the live web, not a cached 403
    monkeypatch.setattr(fetch, "_do_get", _fake(200, "<title>hi</title>"))
    r = fetch.get("https://blocked.test/")
    assert r.status == 200
    assert not r.from_cache


def test_200_cached_and_reread(isolated_cache, monkeypatch):
    monkeypatch.setattr(fetch, "_do_get", _fake(200))
    r1 = fetch.get("https://ok.test/")
    assert r1.status == 200 and not r1.from_cache
    monkeypatch.setattr(fetch, "_do_get", _fake(500))  # must not be called
    r2 = fetch.get("https://ok.test/")
    assert r2.status == 200 and r2.from_cache


def test_404_is_cached(isolated_cache, monkeypatch):
    monkeypatch.setattr(fetch, "_do_get", _fake(404, "Page not found"))
    fetch.get("https://gone.test/")
    assert len(list(isolated_cache.glob("*.json"))) == 1  # real HTTP status, cached


def test_timeout_retried_then_error_not_cached(isolated_cache, monkeypatch):
    calls = []
    def timing_out(url, timeout):
        calls.append(url)
        raise httpx.ReadTimeout("slow", request=httpx.Request("GET", url))
    monkeypatch.setattr(fetch, "_do_get", timing_out)
    r = fetch.get("https://slow.test/")
    assert r.status == 0
    assert r.error and "Timeout" in r.error
    # robots probe (fail-open on timeout, single attempt) + URL + one retry
    assert len(calls) == 3
    assert list(isolated_cache.glob("*.json")) == []


# --- robots.txt ---

def test_robots_disallow_blocks_without_fetching(isolated_cache, monkeypatch):
    calls = []
    def do_get(url, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/",
                                  request=httpx.Request("GET", url))
        return httpx.Response(200, text="hi", request=httpx.Request("GET", url))
    monkeypatch.setattr(fetch, "_do_get", do_get)
    r = fetch.get("https://polite.test/private/menu")
    assert r.status == -1
    assert r.error == "robots_disallowed"
    assert calls == ["https://polite.test/robots.txt"]  # target never fetched
    # non-disallowed path on the same host proceeds (parser memoized)
    r2 = fetch.get("https://polite.test/menu")
    assert r2.status == 200


def test_robots_error_fails_open(isolated_cache, monkeypatch):
    def do_get(url, timeout):
        if url.endswith("/robots.txt"):
            return httpx.Response(500, text="", request=httpx.Request("GET", url))
        return httpx.Response(200, text="hi", request=httpx.Request("GET", url))
    monkeypatch.setattr(fetch, "_do_get", do_get)
    assert fetch.get("https://broken-robots.test/anything").status == 200


# --- binary bodies ---

def test_pdf_body_cached_as_base64(isolated_cache, monkeypatch):
    pdf_bytes = b"%PDF-1.4 fake binary \x00\x01\x02"
    def do_get(url, timeout):
        if url.endswith("/robots.txt"):
            return httpx.Response(404, text="", request=httpx.Request("GET", url))
        return httpx.Response(200, content=pdf_bytes,
                              headers={"content-type": "application/pdf"},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(fetch, "_do_get", do_get)
    r = fetch.get("https://pdfs.test/menu.pdf")
    assert r.content_b64 == base64.standard_b64encode(pdf_bytes).decode()
    assert r.content == pdf_bytes
    assert r.text == ""
    # survives the cache round-trip byte-for-byte
    r2 = fetch.get("https://pdfs.test/menu.pdf")
    assert r2.from_cache and r2.content == pdf_bytes


def test_rendered_fetch_reads_playwright_cache(isolated_cache, monkeypatch):
    # a rendered result cached under the playwright: key is served without
    # importing playwright at all
    fetch._write_cache(
        "playwright:https://js.test/",
        fetch.CachedResponse(url="https://js.test/", status=200,
                             final_url="https://js.test/", headers={},
                             text="<html><body>rendered!</body></html>"),
    )
    r = fetch.get_rendered("https://js.test/")
    assert r.from_cache and r.status == 200 and "rendered!" in r.text
