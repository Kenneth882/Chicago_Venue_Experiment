"""Stage 3 tests — golden cases against cache fixtures + pure-logic units.

ZERO network, ZERO API calls: fetch's HTTP layer is replaced with a guard
that fails any cache miss, and llm.extract_venue_data is replaced with a
recorder that returns the case's recorded response. The golden set doubles
as the regression harness for any future prompt change (re-run with the real
llm layer and compare against expected).
"""

import base64
import io
import json
from pathlib import Path

import httpx
import pytest

from src import db, fetch, llm, stage3_extract
from src.stage3_extract import (
    apply_price_rules,
    discover_candidates,
    html_to_text,
    is_js_shell,
    keyword_match,
    nav_links,
    price_signal,
)

CASES_DIR = Path(__file__).parent / "golden" / "cases"
CASES = sorted(CASES_DIR.glob("*.json"))


# --- fixture generators (binary fixtures built from readable specs) ---

def _menu_image_bytes(lines: list[str], fmt: str) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((60, 80 + i * 90), line, fill="black")
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _entry_body(spec: dict) -> tuple[str, str | None]:
    """-> (text, content_b64) for a web fixture spec."""
    gen = spec.get("generate")
    if gen is None:
        return spec.get("text", ""), None
    if gen["type"] == "pdf_menu":
        raw = _menu_image_bytes(gen["lines"], "PDF")
    elif gen["type"] == "image_menu":
        raw = _menu_image_bytes(gen["lines"], "JPEG")
    else:
        raise ValueError(gen["type"])
    return "", base64.standard_b64encode(raw).decode()


def _write_fixture(key: str, url: str, spec: dict) -> None:
    text, blob = _entry_body(spec)
    fetch._write_cache(key, fetch.CachedResponse(
        url=url,
        status=spec.get("status", 200),
        final_url=spec.get("final_url", url),
        headers={"content-type": spec.get("content_type", "text/html")},
        text=text,
        content_b64=blob,
    ))


class LLMRecorder:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, venue_name, address, *, text=None, images=None,
                 source_urls, dry_run=False):
        assert not dry_run
        self.calls.append({
            "venue_name": venue_name, "address": address, "text": text,
            "images": images or [], "source_urls": source_urls,
        })
        assert self.response is not None, "LLM called but case has no llm_response"
        return dict(self.response)


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """Isolated cache + hard network guard: any cache miss raises ConnectError
    inside fetch (handled as a failed fetch), proving tests never hit the web."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(fetch, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    fetch._last_hit.clear()
    fetch._robots_mem.clear()

    def no_network(url, timeout):
        raise httpx.ConnectError("network disabled in golden tests",
                                 request=httpx.Request("GET", url))
    monkeypatch.setattr(fetch, "_do_get", no_network)
    yield cache_dir


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "golden.db")
    db.init_db(conn)
    yield conn
    conn.close()


def _seed_venue(conn, venue: dict) -> str:
    db.upsert_venue(conn, {
        **venue,
        "rating": 4.5, "user_rating_count": 500, "price_level": 2,
        "business_status": "OPERATIONAL", "types": ["bar"], "primary_type": "bar",
        "found_by": {"cell_id": "c", "query": "q", "rank": 1, "run_id": "r"},
    })
    db.set_stage(conn, venue["place_id"], "2_filtered_ok")
    return venue["place_id"]


# --- golden cases ---

@pytest.mark.parametrize("case_path", CASES, ids=lambda p: p.stem)
def test_golden_case(case_path, offline, conn, monkeypatch):
    case = json.loads(case_path.read_text())
    for url, spec in case.get("web", {}).items():
        _write_fixture(url, url, spec)
    for url, spec in case.get("rendered", {}).items():
        _write_fixture(f"playwright:{url}", url, spec)

    place_id = _seed_venue(conn, case["venue"])
    recorder = LLMRecorder(case.get("llm_response"))
    monkeypatch.setattr(llm, "extract_venue_data", recorder)
    nav_fallback_calls = []
    monkeypatch.setattr(llm, "classify_nav_links",
                        lambda *a, **k: nav_fallback_calls.append(a) or [])

    stage3_extract.run(conn)

    exp = case["expected"]
    row = db.get_venue(conn, place_id)
    assert row["stage"] == exp["stage"]
    assert row["eliminated_reason"] == exp["eliminated_reason"]

    envelope = json.loads(row["extraction_json"])
    assert envelope["menu_unavailable"] == exp["menu_unavailable"]
    assert envelope["flags"] == exp["flags"]
    assert envelope["price_signal"] == exp["price_signal"]

    if exp["llm_called"]:
        assert len(recorder.calls) == 1, "exactly ONE extraction call per venue"
        call = recorder.calls[0]
        assert call["venue_name"] == case["venue"]["name"]
        assert case["venue"]["formatted_address"] in call["address"]
        for needle in exp.get("llm_text_contains", []):
            assert needle in (call["text"] or ""), f"{needle!r} missing from LLM input"
        assert bool(call["images"]) == exp.get("llm_used_vision", False)
        # known values match; every miss stays null — never a coerced number
        assert envelope["extraction"] == case["llm_response"]
        for key, val in case["llm_response"].items():
            if val is None:
                assert envelope["extraction"][key] is None
    else:
        assert recorder.calls == []
        assert envelope["extraction"] is None

    for url in exp.get("sources_include", []):
        assert url in envelope["sources"]

    # idempotency: second run finds nothing to do and changes nothing
    funnel2 = stage3_extract.run(conn)
    assert funnel2["in_2_filtered_ok"] == 0
    assert db.get_venue(conn, place_id)["stage"] == exp["stage"]


def test_dry_run_makes_no_calls_and_no_writes(offline, conn, monkeypatch):
    place_id = _seed_venue(conn, {
        "place_id": "dry_run_venue", "name": "Dry Run Bar",
        "formatted_address": "1 Test St", "website_uri": "https://dryrun.test/",
    })
    def boom(*a, **k):
        raise AssertionError("network/LLM touched during --dry-run")
    monkeypatch.setattr(fetch, "get", boom)
    monkeypatch.setattr(fetch, "get_rendered", boom)
    monkeypatch.setattr(llm, "extract_venue_data", boom)
    funnel = stage3_extract.run(conn, dry_run=True)
    assert funnel["would_process"] == 1
    row = db.get_venue(conn, place_id)
    assert row["stage"] == "2_filtered_ok"
    assert row["extraction_json"] is None


# --- deterministic price rules (never an LLM decision) ---

def _x(**kw):
    base = {"menu_matches_venue": True, "confidence": "high",
            "fnb_minimum_usd": None, "buyout_price_usd": None,
            "semi_private_available": None,
            "cocktail_price_min": None, "cocktail_price_max": None}
    base.update(kw)
    return base


def test_price_rules_kill_over_10k_no_cheaper_option():
    assert apply_price_rules(_x(buyout_price_usd=10001))[0] == "kill"
    assert apply_price_rules(_x(fnb_minimum_usd=12000))[0] == "kill"


def test_price_rules_exactly_10k_is_not_a_kill():
    verdict, flags, _ = apply_price_rules(_x(buyout_price_usd=10000))
    assert verdict == "pass" and flags == []


def test_price_rules_high_buyout_with_cheaper_option_kept_and_flagged():
    for cheaper in (dict(semi_private_available=True), dict(fnb_minimum_usd=4000)):
        verdict, flags, _ = apply_price_rules(_x(buyout_price_usd=25000, **cheaper))
        assert verdict == "pass"
        assert flags == ["buyout_high_but_flexible"]


def test_price_rules_review_outranks_kill():
    verdict, flags, _ = apply_price_rules(
        _x(confidence="low", buyout_price_usd=50000))
    assert verdict == "review" and flags == ["extraction_low_confidence"]
    verdict, flags, _ = apply_price_rules(
        _x(menu_matches_venue=False, buyout_price_usd=50000))
    assert verdict == "review" and flags == ["menu_identity_mismatch"]


def test_price_signal_buckets():
    assert price_signal(_x(fnb_minimum_usd=1500)) == "low"
    assert price_signal(_x(fnb_minimum_usd=5000)) == "mid"
    assert price_signal(_x(fnb_minimum_usd=9000)) == "high"
    assert price_signal(_x(fnb_minimum_usd=15000)) == "very_high"
    assert price_signal(_x(cocktail_price_min=10, cocktail_price_max=12)) == "low"
    assert price_signal(_x(cocktail_price_min=14, cocktail_price_max=18)) == "mid"
    assert price_signal(_x()) is None


# --- content routing helpers ---

def test_is_js_shell():
    assert is_js_shell('<html><body><div id="root"></div><script src="/a.js"></script></body></html>')
    assert not is_js_shell("<html><body><p>" + "real content " * 30 + "</p></body></html>")


def test_html_to_text_decodes_cloudflare_emails():
    real = "events@hawksmoor.com"
    key = 0x42
    encoded = bytes([key] + [ord(c) ^ key for c in real]).hex()
    html = (f'<p>Email <a href="/cdn-cgi/l/email-protection" class="__cf_email__" '
            f'data-cfemail="{encoded}">[email&#160;protected]</a> to book.</p>')
    text = html_to_text(html)
    assert real in text
    assert "protected]" not in text


def test_html_to_text_bad_cfemail_attr_is_ignored():
    assert "hi" in html_to_text('<p data-cfemail="zz">hi</p>')


def test_sanitize_extraction_nulls_junk_email():
    from src.stage3_extract import sanitize_extraction
    x = {"event_contact_email": "[email protected]", "contact_method": "email"}
    assert sanitize_extraction(x) == ["invalid_email_nulled"]
    assert x["event_contact_email"] is None

    ok = {"event_contact_email": "events@venue.com"}
    assert sanitize_extraction(ok) == []
    assert ok["event_contact_email"] == "events@venue.com"

    missing = {"event_contact_email": None}
    assert sanitize_extraction(missing) == []


def test_html_to_text_strips_scripts():
    text = html_to_text("<html><body><p>Menu $14</p><script>var x=1;</script><style>p{}</style></body></html>")
    assert "Menu $14" in text and "var x" not in text


def test_nav_links_same_host_absolute_deduped():
    html = ('<a href="/menu">Menu</a><a href="https://other.test/menu">Other</a>'
            '<a href="/menu#drinks">Menu again</a><a href="mailto:x@y.z">Mail</a>')
    links = nav_links(html, "https://venue.test")
    assert links == [("Menu", "https://venue.test/menu")]


def test_keyword_match():
    assert keyword_match("Private Events", "https://v.test/x")
    assert keyword_match("", "https://v.test/our-cocktails")
    assert not keyword_match("Gift Cards", "https://v.test/gift-cards")


def test_discover_calls_claude_fallback_only_when_keywords_miss(offline, monkeypatch):
    homepage = fetch.CachedResponse(
        url="https://v.test/", status=200, final_url="https://v.test/",
        headers={"content-type": "text/html"},
        text='<a href="/imbibe">Imbibe</a><a href="/soirees">Fetes</a>',
    )
    picked = []
    monkeypatch.setattr(llm, "classify_nav_links",
                        lambda name, links, **k: picked.append(links) or ["https://v.test/imbibe"])
    candidates = discover_candidates("Venue", homepage)
    assert picked, "Claude fallback not consulted"
    # Claude found a real page, so known-path guesses are skipped entirely
    assert candidates == ["https://v.test/imbibe"]


def test_discover_known_paths_only_when_nothing_found(offline, monkeypatch):
    homepage = fetch.CachedResponse(
        url="https://v.test/", status=200, final_url="https://v.test/",
        headers={"content-type": "text/html"}, text="<p>No links at all.</p>",
    )
    monkeypatch.setattr(llm, "classify_nav_links",
                        lambda *a, **k: pytest.fail("no links -> no Claude call"))
    candidates = discover_candidates("Venue", homepage)
    assert candidates == ["https://v.test" + p for p in stage3_extract.KNOWN_PATHS]


def test_discover_skips_known_paths_when_nav_matches(offline):
    homepage = fetch.CachedResponse(
        url="https://v.test/", status=200, final_url="https://v.test/",
        headers={"content-type": "text/html"},
        text='<a href="/food">Food</a><a href="/about">About</a>',
    )
    candidates = discover_candidates("Venue", homepage)
    assert candidates == ["https://v.test/food"]


def test_discover_dedup_is_trailing_slash_insensitive(offline):
    homepage = fetch.CachedResponse(
        url="https://v.test/", status=200, final_url="https://v.test/",
        headers={"content-type": "text/html"},
        text='<a href="/food">Food</a><a href="/food/">Food menu</a>'
             '<a href="/drinks/">Drinks</a>',
    )
    candidates = discover_candidates("Venue", homepage)
    assert candidates == ["https://v.test/food", "https://v.test/drinks/"]


# --- menu-provider follow ---

def test_provider_links_anchors_iframes_subdomains():
    from src.stage3_extract import provider_links
    html = (
        '<a href="https://www.toasttab.com/venue-x">Order</a>'
        '<a href="https://order.toasttab.com/menus/venue-x/">Menu</a>'
        '<iframe src="https://cdn2.getbento.com/venue/menu.pdf"></iframe>'
        '<a href="https://not-a-provider.test/menu">Menu</a>'
        '<a href="https://faketoasttab.com/x">Spoof</a>'
        '<a href="mailto:events@v.test">Email</a>'
        '<a href="https://www.toasttab.com/venue-x/">Order again</a>'
    )
    links = provider_links(html, "https://v.test/")
    assert links == [
        "https://www.toasttab.com/venue-x",
        "https://order.toasttab.com/menus/venue-x/",
        "https://cdn2.getbento.com/venue/menu.pdf",
    ]


def test_provider_links_relative_hrefs_never_match():
    from src.stage3_extract import provider_links
    assert provider_links('<a href="/menu">Menu</a>', "https://v.test/") == []


# --- low-confidence Sonnet retry (model policy, no live API) ---

class _FakeResponse:
    def __init__(self, payload):
        self.content = [type("B", (), {"type": "text", "text": json.dumps(payload)})()]
        self.usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()


def _fake_client(create):
    return type("C", (), {"messages": type("M", (), {"create": staticmethod(create)})()})()


def test_low_confidence_text_extraction_retries_on_sonnet(monkeypatch):
    calls = []
    def create(*, model, **kw):
        calls.append(model)
        conf = "low" if model == llm.MODEL_TEXT else "high"
        return _FakeResponse({"confidence": conf})
    monkeypatch.setattr(llm, "_client", _fake_client(create))
    before = llm.request_count
    result = llm.extract_venue_data("V", "A", text="menu", source_urls=["u"])
    assert calls == [llm.MODEL_TEXT, llm.MODEL_VISION]
    assert result["confidence"] == "high"
    assert llm.request_count == before + 2


def test_low_confidence_retry_result_wins_even_if_still_low(monkeypatch):
    calls = []
    def create(*, model, **kw):
        calls.append(model)
        return _FakeResponse({"confidence": "low", "marker": model})
    monkeypatch.setattr(llm, "_client", _fake_client(create))
    result = llm.extract_venue_data("V", "A", text="menu", source_urls=["u"])
    assert calls == [llm.MODEL_TEXT, llm.MODEL_VISION]
    assert result["marker"] == llm.MODEL_VISION


def test_confident_text_extraction_not_retried(monkeypatch):
    calls = []
    def create(*, model, **kw):
        calls.append(model)
        return _FakeResponse({"confidence": "medium"})
    monkeypatch.setattr(llm, "_client", _fake_client(create))
    llm.extract_venue_data("V", "A", text="menu", source_urls=["u"])
    assert calls == [llm.MODEL_TEXT]


def test_low_confidence_vision_extraction_not_retried(monkeypatch):
    calls = []
    def create(*, model, **kw):
        calls.append(model)
        return _FakeResponse({"confidence": "low"})
    monkeypatch.setattr(llm, "_client", _fake_client(create))
    llm.extract_venue_data("V", "A", images=[("image/jpeg", "aGk=")], source_urls=["u"])
    assert calls == [llm.MODEL_VISION]


def test_extraction_schema_matches_claude_md_fields():
    expected = {
        "menu_matches_venue", "cocktail_price_min", "cocktail_price_max",
        "entree_price_min", "entree_price_max", "fnb_minimum_usd",
        "buyout_price_usd", "semi_private_available", "stated_capacity",
        "event_contact_email", "contact_method", "event_keywords_found",
        "confidence", "evidence",
    }
    assert set(llm.EXTRACTION_SCHEMA["properties"]) == expected
    assert set(llm.EXTRACTION_SCHEMA["required"]) == expected
