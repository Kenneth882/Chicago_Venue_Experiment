"""Stage 3 menu deep-dive — Milestone 4. The only LLM-heavy stage.

CLI: python -m src.stage3_extract [--limit N] [--dry-run]

Per venue at stage 2_filtered_ok:
  1. homepage via fetch.get (cached from Stage 2); Playwright fallback for
     403/JS-shell pages
  2. discover menu/private-event pages: sitemap.xml grep -> nav-link keyword
     match -> Claude fallback (only when keywords find nothing) -> known-path
     guesses only when all of those find nothing
  3. route by content type: HTML -> clean text (bs4); PDF -> images
     (pdf2image); images -> downscale ~1500px (Pillow)
  3b. menu-provider follow: links/iframes to menu-hosting platforms (Toast,
      BentoBox, Popmenu, ...) found on any fetched page are fetched too —
      the hosted page holds the actual menu, which never enters the venue
      page DOM even after rendering
  4. ONE llm.extract_venue_data call per venue (text model, vision model when
     images are present). No menu content found -> menu_unavailable flag,
     NO LLM call, venue is NOT eliminated
  5. deterministic price rules IN CODE (see apply_price_rules)
  6. write the extraction envelope to extraction_json; stage transitions:
     pass -> 3_enriched | kill -> eliminated/flag_price_too_high |
     low confidence / menu mismatch -> needs_review (marker in reason column)

--dry-run: zero network, zero LLM calls, zero DB writes.
"""

from __future__ import annotations

import argparse
import base64
import io
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from src import db, fetch, llm

logger = logging.getLogger(__name__)

KNOWN_PATHS = [
    "/menu", "/menus", "/food", "/drinks", "/cocktails",
    "/private-events", "/events", "/parties", "/groups", "/book",
]
MENU_KEYWORDS = (
    "menu", "food", "drink", "cocktail", "dinner", "brunch", "lunch", "wine",
    "beer", "private", "event", "party", "parties", "group", "book",
    "reservation", "celebrate", "gather",
)
# Menu-hosting platforms: the venue site links out (or iframes) to these and
# the menu itself lives on THEIR page — following the link is the only way to
# see prices. Matched by host suffix. Deterministic list, code-decided.
MENU_PROVIDER_DOMAINS = (
    "toasttab.com",        # Toast
    "getbento.com",        # BentoBox
    "popmenu.com",         # Popmenu
    "grubhub.com", "seamless.com",
    "doordash.com", "ubereats.com",
    "chownow.com",
    "ezcater.com",
    "spotapps.co",         # SpotHopper
    "singleplatform.com",
    "untappd.com",         # brewery tap lists
)
MAX_PROVIDER_PAGES = 2     # hosted menu pages fetched per venue
MAX_CONTENT_PAGES = 5      # menu/event pages fed to extraction, besides homepage
MAX_FETCH_ATTEMPTS = 14    # candidate URLs tried per venue
MAX_TEXT_CHARS = 24_000    # total extraction text budget
MAX_IMAGES = 4             # menu images per extraction call
MAX_PDF_PAGES = 3
IMAGE_MAX_PX = 1500
JS_SHELL_TEXT_CHARS = 200  # visible chars below this = JS shell

PRICE_CEILING_USD = 10_000

STAGE_IN = "2_filtered_ok"
STAGE_OUT = "3_enriched"


# --- content helpers (pure, unit-tested) ---

def _decode_cfemail(hexstr: str) -> str:
    """Cloudflare email obfuscation: first byte is the XOR key for the rest."""
    data = bytes.fromhex(hexstr)
    return bytes(b ^ data[0] for b in data[1:]).decode("utf-8", "replace")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    # Cloudflare-protected emails render as "[email protected]"; the real
    # address is recoverable from data-cfemail — decode it so extraction
    # sees the actual contact address.
    for el in soup.find_all(attrs={"data-cfemail": True}):
        try:
            el.string = _decode_cfemail(el["data-cfemail"])
        except (ValueError, IndexError):
            pass
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def is_js_shell(html: str) -> bool:
    return len(html_to_text(html)) < JS_SHELL_TEXT_CHARS


def nav_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(text, absolute href) for same-host <a> tags, deduped, order kept."""
    soup = BeautifulSoup(html or "", "html.parser")
    host = urlparse(base_url).netloc
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https") or parsed.netloc != host:
            continue
        href = href.split("#")[0]
        if href in seen or href.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(href)
        out.append((a.get_text(" ", strip=True), href))
    return out


def keyword_match(text: str, href: str) -> bool:
    haystack = f"{text} {urlparse(href).path}".lower()
    return any(kw in haystack for kw in MENU_KEYWORDS)


def provider_links(html: str, base_url: str) -> list[str]:
    """<a>/<iframe> URLs pointing at known menu-hosting platforms, deduped
    (trailing-slash insensitive), order kept. These are external, so
    nav_links never surfaces them."""
    soup = BeautifulSoup(html or "", "html.parser")
    seen, out = set(), []
    for tag, attr in (("a", "href"), ("iframe", "src")):
        for el in soup.find_all(tag, **{attr: True}):
            url = urljoin(base_url, el[attr].strip()).split("#")[0]
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                continue
            host = parsed.netloc.lower()
            if not any(host == d or host.endswith("." + d) for d in MENU_PROVIDER_DOMAINS):
                continue
            key = url.rstrip("/")
            if key not in seen:
                seen.add(key)
                out.append(url)
    return out


def downscale_image(data: bytes, max_px: int = IMAGE_MAX_PX) -> tuple[str, str]:
    """-> (media_type, base64) JPEG capped at max_px on the long edge."""
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode()


def pdf_to_images(data: bytes) -> list[tuple[str, str]]:
    from pdf2image import convert_from_bytes
    pages = convert_from_bytes(data, dpi=150, first_page=1, last_page=MAX_PDF_PAGES)
    out = []
    for page in pages:
        buf = io.BytesIO()
        page.save(buf, format="JPEG", quality=80)
        out.append(downscale_image(buf.getvalue()))
    return out


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def sanitize_extraction(x: dict[str, Any]) -> list[str]:
    """Null out junk the LLM faithfully copied from the page — e.g. a
    Cloudflare-obfuscated '[email protected]' that survived because the raw
    HTML never reached html_to_text (image/PDF path). Returns flags."""
    email = x.get("event_contact_email")
    if email is not None and not EMAIL_RE.match(email):
        x["event_contact_email"] = None
        return ["invalid_email_nulled"]
    return []


# --- deterministic price rules (pure, unit-tested; NEVER an LLM decision) ---

def apply_price_rules(x: dict[str, Any]) -> tuple[str, list[str], Optional[str]]:
    """-> (verdict, flags, price_signal). verdict: pass | kill | review.

    Review outranks kill: low confidence or a menu mismatch goes to a human,
    per CLAUDE.md ("needs_review queue, not pass/fail")."""
    flags: list[str] = []

    if x.get("menu_matches_venue") is False:
        return "review", ["menu_identity_mismatch"], None
    if x.get("confidence") == "low":
        return "review", ["extraction_low_confidence"], price_signal(x)

    fnb = x.get("fnb_minimum_usd")
    buyout = x.get("buyout_price_usd")
    high_min = fnb is not None and fnb > PRICE_CEILING_USD
    high_buyout = buyout is not None and buyout > PRICE_CEILING_USD
    cheaper_option = (x.get("semi_private_available") is True) or (
        fnb is not None and fnb <= PRICE_CEILING_USD
    )

    if high_min or high_buyout:
        if cheaper_option:
            flags.append("buyout_high_but_flexible")
        else:
            return "kill", ["flag_price_too_high"], price_signal(x)

    return "pass", flags, price_signal(x)


def price_signal(x: dict[str, Any]) -> Optional[str]:
    """fnb_minimum bucket, else median cocktail price tier (CLAUDE.md)."""
    fnb = x.get("fnb_minimum_usd")
    if fnb is not None:
        if fnb < 2_000:
            return "low"
        if fnb <= 5_000:
            return "mid"
        if fnb <= 10_000:
            return "high"
        return "very_high"
    lo, hi = x.get("cocktail_price_min"), x.get("cocktail_price_max")
    if lo is not None and hi is not None:
        med = (lo + hi) / 2
        if med < 12:
            return "low"
        if med <= 16:
            return "mid"
        if med <= 20:
            return "high"
        return "very_high"
    return None


# --- fetching with fallback ---

def fetch_page(url: str) -> fetch.CachedResponse:
    """httpx first; Playwright fallback on bot-block or JS shell."""
    resp = fetch.get(url)
    content_type = resp.headers.get("content-type", "")
    needs_render = resp.status in fetch.RETRY_STATUSES or (
        resp.status == 200 and "html" in content_type.lower() and is_js_shell(resp.text)
    )
    if needs_render:
        rendered = fetch.get_rendered(url)
        if rendered.status == 200:
            return rendered
    return resp


# --- discovery ---

def sitemap_candidates(base: str) -> list[str]:
    resp = fetch.get(f"{base}/sitemap.xml")
    if resp.status != 200:
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text)
    host = urlparse(base).netloc
    return [
        u for u in locs
        if urlparse(u).netloc == host and keyword_match("", u)
    ][:10]


def discover_candidates(
    venue_name: str, homepage: fetch.CachedResponse, *, dry_run: bool = False
) -> list[str]:
    """Candidate menu/event URLs: sitemap grep, nav-link keyword match,
    Claude fallback only when keywords find nothing. Known-path guesses only
    when all of those find nothing — guessed paths mostly 404 and waste
    fetches/tokens once real pages are known. Dedup is trailing-slash
    insensitive (/food and /food/ are one fetch, one LLM input)."""
    parsed = urlparse(homepage.final_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    candidates = sitemap_candidates(base)

    links = nav_links(homepage.text, base)
    matched = [href for text, href in links if keyword_match(text, href)]
    candidates += matched
    if not matched and links:
        candidates += llm.classify_nav_links(venue_name, links, dry_run=dry_run)

    if not candidates:
        candidates = [base + p for p in KNOWN_PATHS]

    seen, out = set(), []
    home = homepage.final_url.rstrip("/")
    for url in candidates:
        key = url.rstrip("/")
        if key != home and key not in seen:
            seen.add(key)
            out.append(url)
    return out


# --- per-venue pipeline ---

def _consume_content(
    resp: fetch.CachedResponse,
    parts: list[str],
    images: list[tuple[str, str]],
    label: str,
) -> bool:
    """Route one fetched page into extraction input by content type.
    Returns True only if something was actually consumed."""
    content_type = resp.headers.get("content-type", "").lower()
    if "pdf" in content_type:
        pages = pdf_to_images(resp.content)[: MAX_IMAGES - len(images)]
        if not pages:
            return False
        images.extend(pages)
    elif content_type.startswith("image/"):
        if len(images) >= MAX_IMAGES:
            return False
        images.append(downscale_image(resp.content))
    elif resp.text:
        parts.append(f"[{label} {resp.final_url}]\n{html_to_text(resp.text)}")
    else:
        return False
    return True


def process_venue(
    conn: Any, venue: Any, run_id: str, funnel: Counter, *, dry_run: bool = False
) -> None:
    place_id, name = venue["place_id"], venue["name"]
    website = venue["website_uri"]

    if dry_run:
        logger.info("DRY RUN | %s | %s | would deep-dive %s", place_id, name, website)
        funnel["would_process"] += 1
        return

    homepage = fetch_page(website)
    sources = [website]
    home_parts: list[str] = []
    provider_parts: list[str] = []
    content_parts: list[str] = []
    images: list[tuple[str, str]] = []

    if homepage.status == 200 and homepage.text:
        home_parts.append(f"[PAGE {homepage.final_url}]\n{html_to_text(homepage.text)}")

    seen_pages = {homepage.final_url.rstrip("/")}
    provider_seen: set[str] = set()
    provider_urls: list[str] = []

    def collect_providers(html: str, page_url: str) -> None:
        for u in provider_links(html, page_url):
            if u.rstrip("/") not in provider_seen:
                provider_seen.add(u.rstrip("/"))
                provider_urls.append(u)

    content_pages = 0
    if homepage.status == 200:
        collect_providers(homepage.text, homepage.final_url)
        for url in discover_candidates(name, homepage)[:MAX_FETCH_ATTEMPTS]:
            if content_pages >= MAX_CONTENT_PAGES or len(images) >= MAX_IMAGES:
                break
            resp = fetch_page(url)
            if resp.status != 200 or resp.final_url.rstrip("/") in seen_pages:
                continue  # dead, or a redirect back to a page already read
            try:
                if not _consume_content(resp, content_parts, images, "PAGE"):
                    continue
            except Exception as exc:
                logger.info("content routing failed | %s | %s | %s", place_id, url, exc)
                continue
            if resp.text:
                collect_providers(resp.text, resp.final_url)
            seen_pages.add(resp.final_url.rstrip("/"))
            sources.append(url)
            content_pages += 1

        # Menu-hosting platforms (Toast, BentoBox, ...): the menu lives on
        # THEIR page, never in the venue DOM. Separate cap — a venue whose
        # own pages have no prices still needs its hosted menu. Counts as
        # content, so a hosted-menu venue is never menu_unavailable.
        for url in provider_urls[:MAX_PROVIDER_PAGES]:
            resp = fetch_page(url)
            if resp.status != 200 or resp.final_url.rstrip("/") in seen_pages:
                continue
            try:
                if not _consume_content(resp, provider_parts, images, "HOSTED MENU PAGE"):
                    continue
            except Exception as exc:
                logger.info("content routing failed | %s | %s | %s", place_id, url, exc)
                continue
            seen_pages.add(resp.final_url.rstrip("/"))
            sources.append(url)
            content_pages += 1
            funnel["provider_pages"] += 1

    # Hosted menu pages go right after the homepage so the text budget can
    # never truncate the actual menu behind less useful site pages.
    text_parts = home_parts + provider_parts + content_parts

    envelope: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "menu_unavailable": False,
        "extraction": None,
        "flags": [],
        "price_signal": None,
    }

    # No menu/event page found anywhere -> flag, don't extract, don't kill.
    if content_pages == 0 and not images:
        envelope["menu_unavailable"] = True
        db.write_extraction(conn, place_id, envelope)
        db.set_stage(conn, place_id, STAGE_OUT)
        funnel["menu_unavailable"] += 1
        logger.info("%s | %s | menu_unavailable (flag, kept)", place_id, name)
        return

    text_blob = "\n\n".join(text_parts)[:MAX_TEXT_CHARS]
    extraction = llm.extract_venue_data(
        name,
        venue["formatted_address"] or "",
        text=text_blob or None,
        images=images,
        source_urls=sources,
    )
    sanitize_flags = sanitize_extraction(extraction)
    verdict, flags, signal = apply_price_rules(extraction)
    envelope.update(extraction=extraction, flags=flags + sanitize_flags,
                    price_signal=signal)
    db.write_extraction(conn, place_id, envelope)

    if verdict == "kill":
        db.set_stage(conn, place_id, "eliminated", "flag_price_too_high")
        funnel["flag_price_too_high"] += 1
        logger.info("%s | %s | flag_price_too_high", place_id, name)
    elif verdict == "review":
        marker = flags[0] if flags else "extraction_needs_review"
        db.set_stage(conn, place_id, "needs_review", marker)
        funnel[marker] += 1
        logger.info("%s | %s | needs_review (%s)", place_id, name, marker)
    else:
        db.set_stage(conn, place_id, STAGE_OUT)
        funnel["enriched"] += 1
        logger.info("%s | %s | 3_enriched (signal=%s flags=%s)", place_id, name, signal, flags)


def run(
    conn: Any,
    *,
    limit: Optional[int] = None,
    dry_run: bool = False,
    place_ids: Optional[list[str]] = None,
) -> Counter:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if place_ids:
        # explicit selection (e.g. re-running a smoke set); still gated on
        # STAGE_IN so an eliminated/needs_review row is never re-processed
        rows = [
            row for pid in place_ids
            if (row := db.get_venue(conn, pid)) is not None and row["stage"] == STAGE_IN
        ]
    else:
        rows = db.venues_at_stage(conn, STAGE_IN, limit)
    funnel: Counter = Counter()
    funnel["in_2_filtered_ok"] = len(rows)
    for venue in rows:
        try:
            process_venue(conn, venue, run_id, funnel, dry_run=dry_run)
        except Exception:
            logger.exception("stage3 failed | %s | %s", venue["place_id"], venue["name"])
            funnel["errors"] += 1
    return funnel


def print_funnel(funnel: Counter, dry_run: bool) -> None:
    print(f"\nfunnel — in (2_filtered_ok): {funnel['in_2_filtered_ok']}")
    if dry_run:
        print(f"  would process (dry run): {funnel['would_process']}")
        return
    for key in ("enriched", "menu_unavailable", "flag_price_too_high",
                "extraction_low_confidence", "menu_identity_mismatch", "errors"):
        if funnel[key]:
            print(f"  {key:<28} {funnel[key]:>5}")
    print(f"out (3_enriched incl. menu_unavailable): "
          f"{funnel['enriched'] + funnel['menu_unavailable']}")
    if funnel["provider_pages"]:
        print(f"(hosted menu pages fetched: {funnel['provider_pages']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 3 menu deep-dive")
    parser.add_argument("--limit", type=int, default=None, help="max venues to process")
    parser.add_argument("--place-id", action="append", dest="place_ids", default=None,
                        metavar="PLACE_ID",
                        help="process only this venue (repeatable); must be at 2_filtered_ok")
    parser.add_argument("--dry-run", action="store_true",
                        help="zero network, zero LLM calls, zero DB writes")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    conn = db.connect()
    db.init_db(conn)
    funnel = run(conn, limit=args.limit, dry_run=args.dry_run, place_ids=args.place_ids)
    print_funnel(funnel, args.dry_run)
    print(f"LLM calls made: {llm.request_count}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
