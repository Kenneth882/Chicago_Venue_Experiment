"""All Anthropic API calls: models, prompts, schemas in one place.

LLMs read, code decides: this module only (a) extracts venue/menu data into
the strict JSON schema from CLAUDE.md and (b) classifies ambiguous nav links.
No thresholds or pass/fail logic here, ever — that lives in stage3_extract.py.

Structured outputs (output_config.format) guarantee schema-valid JSON, so the
"null over guessing" rule is enforced by the schema (nullable fields) and the
prompt, and callers never need to repair output.

Model policy (CLAUDE.md): text extraction on Haiku-class, vision (PDF/photo
menus) on Sonnet-class. Batch API for the full run is a later work order.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

MODEL_TEXT = "claude-haiku-4-5"
MODEL_VISION = "claude-sonnet-5"
MAX_TOKENS = 2048

# Real API calls made this process (never incremented on dry_run).
request_count = 0

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _nullable(*types: str) -> dict[str, Any]:
    return {"anyOf": [{"type": t} for t in types] + [{"type": "null"}]}


# The exact Stage 3 schema from CLAUDE.md. Every field required; misses are
# null by schema, never absent and never guessed.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "menu_matches_venue",
        "cocktail_price_min", "cocktail_price_max",
        "entree_price_min", "entree_price_max",
        "fnb_minimum_usd", "buyout_price_usd",
        "semi_private_available", "stated_capacity",
        "event_contact_email", "contact_method",
        "event_keywords_found", "confidence", "evidence",
    ],
    "properties": {
        "menu_matches_venue": {"type": "boolean"},
        "cocktail_price_min": _nullable("number"),
        "cocktail_price_max": _nullable("number"),
        "entree_price_min": _nullable("number"),
        "entree_price_max": _nullable("number"),
        "fnb_minimum_usd": _nullable("number"),
        "buyout_price_usd": _nullable("number"),
        "semi_private_available": _nullable("boolean"),
        "stated_capacity": _nullable("integer"),
        "event_contact_email": _nullable("string"),
        "contact_method": {"enum": ["email", "form_only", "phone_only", "none"]},
        "event_keywords_found": {"type": "array", "items": {"type": "string"}},
        "confidence": {"enum": ["high", "medium", "low"]},
        "evidence": {"type": "string"},
    },
}

EXTRACTION_PROMPT = """\
You are extracting structured data about a Chicago venue for an events team.

Venue: {name}
Address: {address}
Content sources: {sources}

The content below (text and/or menu images) was fetched from this venue's \
website. First verify the content actually belongs to THIS venue \
(menu_matches_venue) — hosting platforms sometimes serve the wrong menu.

Extract:
- cocktail and entree price ranges actually printed in the menu
- private-event terms: food & beverage minimum (USD), buyout price (USD),
  whether semi-private space is available, stated capacity (people)
- how to contact for events: a direct email if printed, otherwise whether
  there is only a form, only a phone number, or nothing
- event_keywords_found: verbatim phrases like "private events", "semi-private",
  "buyouts", "group dining" that appear in the content

STRICT RULES:
- NEVER guess or estimate a number. If a price, minimum, or capacity is not
  explicitly stated in the content, use null. A miss must be null.
- evidence: one sentence citing where each extracted number came from
  (which page/section). If nothing was extracted, say so.
- confidence: high only when numbers were clearly printed; low when the
  content was thin, garbled, or possibly the wrong venue.
"""

NAV_LINKS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["menu_or_event_links"],
    "properties": {
        "menu_or_event_links": {"type": "array", "items": {"type": "string"}},
    },
}

NAV_LINKS_PROMPT = """\
Below are navigation links from the website of "{name}", a Chicago venue.
Return the hrefs (verbatim, from the list) most likely to lead to a food or
drink MENU or PRIVATE EVENT / group dining information. Return at most 4.
Return an empty list if none apply.

Links (text -> href):
{links}
"""


def _parse_structured(response) -> dict[str, Any]:
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def extract_venue_data(
    venue_name: str,
    address: str,
    *,
    text: Optional[str] = None,
    images: Optional[list[tuple[str, str]]] = None,  # (media_type, base64)
    source_urls: list[str],
    dry_run: bool = False,
) -> Optional[dict[str, Any]]:
    """ONE extraction call per venue. Returns the schema dict, or None on
    dry_run. Vision model when images are present, else text model."""
    global request_count
    images = images or []
    model = MODEL_VISION if images else MODEL_TEXT
    if dry_run:
        logger.info(
            "DRY RUN — would call %s for %r (text_chars=%d, images=%d)",
            model, venue_name, len(text or ""), len(images),
        )
        return None

    prompt = EXTRACTION_PROMPT.format(
        name=venue_name, address=address, sources=", ".join(source_urls)
    )
    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
        for media_type, data in images
    ]
    body = prompt + ("\n\n--- WEBSITE CONTENT ---\n" + text if text else "")
    content.append({"type": "text", "text": body})

    request_count += 1
    response = _get_client().messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    result = _parse_structured(response)
    logger.info(
        "extraction | %s | model=%s | confidence=%s | in=%d out=%d tokens",
        venue_name, model, result.get("confidence"),
        response.usage.input_tokens, response.usage.output_tokens,
    )
    return result


def classify_nav_links(
    venue_name: str,
    links: list[tuple[str, str]],  # (link text, absolute href)
    *,
    dry_run: bool = False,
) -> list[str]:
    """Claude fallback when deterministic nav-link keyword matching finds
    nothing. Returns hrefs (subset of the input)."""
    global request_count
    if not links:
        return []
    if dry_run:
        logger.info("DRY RUN — would classify %d nav links for %r", len(links), venue_name)
        return []

    listing = "\n".join(f"{t[:60]!r} -> {h}" for t, h in links[:40])
    request_count += 1
    response = _get_client().messages.create(
        model=MODEL_TEXT,
        max_tokens=512,
        output_config={"format": {"type": "json_schema", "schema": NAV_LINKS_SCHEMA}},
        messages=[{
            "role": "user",
            "content": NAV_LINKS_PROMPT.format(name=venue_name, links=listing),
        }],
    )
    hrefs = {h for _, h in links}
    picked = [h for h in _parse_structured(response)["menu_or_event_links"] if h in hrefs]
    logger.info("nav-link fallback | %s | picked %d links", venue_name, len(picked))
    return picked[:4]
