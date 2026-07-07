"""fetch.py cache and retry behavior — httpx layer monkeypatched, no network."""

import httpx
import pytest

from src import fetch


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    fetch._last_hit.clear()
    yield tmp_path


def _fake(status, text="body"):
    def do_get(url, timeout):
        return httpx.Response(status, text=text, request=httpx.Request("GET", url))
    return do_get


def test_403_retried_once_and_not_cached(isolated_cache, monkeypatch):
    calls = []
    def counting(url, timeout):
        calls.append(url)
        return httpx.Response(403, text="Just a moment...", request=httpx.Request("GET", url))
    monkeypatch.setattr(fetch, "_do_get", counting)
    r = fetch.get("https://blocked.test/")
    assert r.status == 403
    assert len(calls) == 2  # one retry after the 403
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
    assert len(calls) == 2
    assert list(isolated_cache.glob("*.json")) == []
