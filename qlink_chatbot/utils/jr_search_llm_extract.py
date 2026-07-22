"""LLM structured extraction for product search.

Modes (SEARCH_EXTRACTION_MODE):
  regex       — regex/alias parsing only (default)
  hybrid      — LLM when regex is weak or price looks suspicious
  llm_primary — LLM first on every search, regex fills gaps
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from openai import AsyncOpenAI

from qlink_chatbot.utils.jr_search_aliases import (
    CATALOG_TAG_ALIASES,
    CATALOG_TAG_KEYS,
    COLOR_ALIASES,
    CONSTRUCTION_KEYWORDS,
    MATERIAL_KEYWORDS,
    PATTERN_ALIASES,
    ROOM_KEYWORDS,
    SHAPE_ALIASES,
    SIZE_CATEGORIES,
    normalise_catalog_tag,
    normalise_size_category,
    SIZE_PATTERN,
    color_search_terms,
)
from qlink_chatbot.utils.jr_search_currency import (
    CURRENCY_FIELDS,
    extract_price_filter_from_text,
    is_price_filter_suspicious,
    normalize_currency_code,
)
from qlink_chatbot.utils.jr_search_keywords import normalise_keyword
from qlink_chatbot.utils.jr_search_sizes import is_cm_dimensions
from qlink_chatbot.utils.logger_config import logger

API_KEY = os.getenv("OPENAI_API_KEY")
_client = AsyncOpenAI(api_key=API_KEY) if API_KEY else None

_VALID_EXTRACTION_MODES = frozenset({"regex", "hybrid", "llm_primary"})


def get_search_extraction_mode() -> str:
    """Resolve extraction mode from env (with legacy bool fallback)."""
    mode = (os.getenv("SEARCH_EXTRACTION_MODE") or "").strip().lower()
    if mode in _VALID_EXTRACTION_MODES:
        return mode
    if os.getenv("SEARCH_LLM_EXTRACTION_ENABLED", "false").lower() == "true":
        return "hybrid"
    return "regex"


# Legacy flag — prefer SEARCH_EXTRACTION_MODE
SEARCH_LLM_EXTRACTION_ENABLED = get_search_extraction_mode() != "regex"

SEARCH_EXTRACTION_SCHEMA = {
    "format": {
        "type": "json_schema",
        "name": "search_attribute_extraction_v2",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "colors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "User exact color words from catalog, e.g. blue, red.",
                },
                "shapes": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "sizes_ft": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Foot dimensions like 8x10.",
                },
                "sizes_cm": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CM dimensions like 200x300.",
                },
                "size_categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Catalog size bucket: small, medium, large, or oversize.",
                },
                "materials": {"type": "array", "items": {"type": "string"}},
                "constructions": {"type": "array", "items": {"type": "string"}},
                "patterns": {"type": "array", "items": {"type": "string"}},
                "rooms": {"type": "array", "items": {"type": "string"}},
                "catalog_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Merchandising facets: new, bestseller, antique, swatch, outdoor.",
                },
                "multicolor": {"type": "boolean"},
                "weight_max_kg": {"type": ["number", "null"]},
                "has_price_filter": {
                    "type": "boolean",
                    "description": "True when user mentions budget, price, above, under, between.",
                },
                "price_currency": {
                    "type": ["string", "null"],
                    "description": "ISO currency: INR, USD, EUR, GBP, AUD, CHF, SGD, AED.",
                },
                "price_type": {
                    "type": "string",
                    "enum": ["none", "gte", "lte", "range"],
                    "description": "gte=above/over/min, lte=under/below/budget, range=between X and Y.",
                },
                "price_amount": {
                    "type": ["number", "null"],
                    "description": "Single bound for gte/lte AFTER expanding shorthand (50k=50000).",
                },
                "price_min": {
                    "type": ["number", "null"],
                    "description": "Range minimum AFTER expanding shorthand.",
                },
                "price_max": {
                    "type": ["number", "null"],
                    "description": "Range maximum AFTER expanding shorthand.",
                },
                "price_raw_phrase": {
                    "type": ["string", "null"],
                    "description": "Exact price phrase from user, e.g. '50k usd', 'under 2 lakh'.",
                },
                "sku": {"type": ["string", "null"]},
                "collection": {"type": ["string", "null"]},
                "refinement": {
                    "type": "string",
                    "enum": ["new", "refine_previous", "show_more"],
                },
            },
            "required": [
                "colors", "shapes", "sizes_ft", "sizes_cm", "size_categories", "materials",
                "constructions", "patterns", "rooms", "catalog_tags", "multicolor",
                "weight_max_kg", "has_price_filter", "price_currency", "price_type",
                "price_amount", "price_min", "price_max", "price_raw_phrase",
                "sku", "collection", "refinement",
            ],
            "additionalProperties": False,
        },
    }
}

_COLOR_KEYS_LOWER = {k.lower(): k for k in COLOR_ALIASES}
_SHAPE_KEYS_LOWER = {k.lower(): k for k in SHAPE_ALIASES}
_PATTERN_KEYS_LOWER = {k.lower(): k for k in PATTERN_ALIASES}


def serialise_for_json(obj: Any) -> Any:
    """Convert sets and other non-JSON types for websocket debug payloads."""
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, dict):
        return {k: serialise_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialise_for_json(v) for v in obj]
    return obj


def summarise_normalise_result(
    normalise_result: tuple,
) -> dict[str, Any]:
    """Unpack normalise_keyword() tuple into a dict for weak-detection."""
    price_filter, clean_keyword, color_check_terms, attribute_filters = normalise_result
    has_attrs = any(
        attribute_filters.get(key)
        for key in (
            "color", "color_exact", "shape", "size", "size_cm", "size_category",
            "material", "construction", "pattern", "room", "catalog_tag",
            "weight_max", "multicolor",
        )
    )
    return {
        "price_filter": price_filter,
        "clean_keyword": clean_keyword or "",
        "color_check_terms": color_check_terms,
        "attribute_filters": attribute_filters,
        "has_attrs": has_attrs,
    }


_REFINEMENT_HINT = re.compile(
    r"\b(also|instead|same|that|those|these|from (the )?last|more like|show more|"
    r"but (in|with|for)|keep (the|my))\b",
    re.IGNORECASE,
)


def _should_ignore_previous_search(keyword: str, user_message: str = "") -> bool:
    """True for fresh mega-menu browses (new arrival / bestsellers / …).

    Prevents previous purple/size/material filters from being AND-merged into
    a brand-new category ask.
    """
    text = (user_message or keyword or "").strip()
    if not text:
        return False
    if _REFINEMENT_HINT.search(text):
        return False
    _pf, _clean, _colors, attrs = normalise_keyword(text)
    return bool(attrs.get("catalog_tag"))


def _sanitize_attrs_to_current_message(
    attrs: dict,
    *,
    keyword: str,
    user_message: str,
) -> dict:
    """Drop previous-search / polluted tool-keyword bleed; keep current ask only."""
    # Prefer the human message — agent tool keyword often reuses a prior tag ("new").
    current = (user_message or keyword or "").strip()
    clean: dict[str, Any] = {
        "colors": [],
        "shapes": [],
        "sizes_ft": [],
        "sizes_cm": [],
        "size_categories": [],
        "materials": [],
        "constructions": [],
        "patterns": [],
        "rooms": [],
        "catalog_tags": [],
        "multicolor": False,
        "weight_max_kg": None,
        "price": None,
        "sku": None,
        "collection": None,
        "refinement": "new",
    }
    clean, notes = backfill_attrs_from_regex(
        clean,
        keyword="",
        user_message=current,
    )
    # Keep LLM catalog_tags only if they appear in the current user ask.
    _pf, _clean, _colors, current_attrs = normalise_keyword(current)
    allowed_tags = set(current_attrs.get("catalog_tag") or set())
    for tag in attrs.get("catalog_tags") or []:
        if tag in allowed_tags and tag not in clean["catalog_tags"]:
            clean["catalog_tags"].append(tag)
    if not clean["catalog_tags"] and allowed_tags:
        clean["catalog_tags"] = sorted(allowed_tags)

    # Preserve price when the current message has budget intent.
    if attrs.get("price") and _source_has_price_intent(current):
        clean["price"] = attrs["price"]
    elif _source_has_price_intent(current):
        pf, _residual = extract_price_filter_from_text(current.lower())
        if pf:
            clean["price"] = _mongo_filter_to_internal_price(pf)

    if notes:
        logger.info(f"[LLM-EXTRACT] sanitized fresh catalog_tag browse: {notes}")
    return clean


def _alias_hit_in_keyword(keyword: str) -> bool:
    """True if keyword contains a known catalog alias token."""
    lower = (keyword or "").lower()
    for alias in sorted(COLOR_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            return True
    for alias in sorted(SHAPE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            return True
    for alias in sorted(PATTERN_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            return True
    for kw in MATERIAL_KEYWORDS:
        if kw in lower:
            return True
    for kw in CONSTRUCTION_KEYWORDS:
        if kw in lower:
            return True
    for room in ROOM_KEYWORDS:
        if room in lower:
            return True
    if SIZE_PATTERN.search(lower):
        return True
    return False


def _source_text_for_extraction(keyword: str, user_message: str = "") -> str:
    return (user_message or keyword or "").strip()


def is_weak_extraction(
    keyword: str,
    normalise_result: tuple | None = None,
    *,
    user_message: str = "",
    extraction_mode: str | None = None,
) -> bool:
    """Return True when hybrid mode should run LLM structured extraction."""
    mode = extraction_mode or get_search_extraction_mode()
    if mode != "hybrid":
        return False

    kw = (keyword or "").strip()
    source = _source_text_for_extraction(kw, user_message)
    if not kw and not source:
        return True

    if "&" in kw or "||" in kw:
        return False

    if normalise_result is None:
        normalise_result = normalise_keyword(kw or source)
    summary = summarise_normalise_result(normalise_result)

    if summary["price_filter"]:
        if is_price_filter_suspicious(source, summary["price_filter"]):
            return True
        return False
    if summary["has_attrs"] and summary["clean_keyword"]:
        return False

    if summary["clean_keyword"] and _alias_hit_in_keyword(summary["clean_keyword"]):
        return False

    return True


def should_run_llm_extraction(
    keyword: str,
    *,
    user_message: str = "",
    skip_llm_extraction: bool = False,
    extraction_mode: str | None = None,
) -> tuple[bool, str]:
    """Return (run_llm, reason) for the current extraction mode."""
    if skip_llm_extraction:
        return False, "skip_flag"

    mode = extraction_mode or get_search_extraction_mode()
    if mode == "regex":
        return False, "mode_regex"

    kw = (keyword or "").strip()
    source = _source_text_for_extraction(kw, user_message)
    normalise_result = normalise_keyword(kw or source)
    summary = summarise_normalise_result(normalise_result)

    if mode == "llm_primary":
        return True, "mode_llm_primary"

    if is_weak_extraction(kw, normalise_result, user_message=user_message, extraction_mode=mode):
        return True, "weak_or_suspicious_price"
    return False, "strong_regex"


def _match_keyword(value: str, keywords: tuple[str, ...]) -> str | None:
    lower = (value or "").lower().strip()
    if not lower:
        return None
    for kw in keywords:
        if lower == kw:
            return kw
    for kw in sorted(keywords, key=len, reverse=True):
        if kw in lower:
            return kw
    return None


def _match_material(value: str) -> str | None:
    return _match_keyword(value, MATERIAL_KEYWORDS)


def _match_construction(value: str) -> str | None:
    return _match_keyword(value, CONSTRUCTION_KEYWORDS)


def _match_room(value: str) -> str | None:
    return _match_keyword(value, ROOM_KEYWORDS)


def _match_catalog_tag(value: str) -> str | None:
    return normalise_catalog_tag(value)


def _validate_size_ft(value: str) -> str | None:
    text = (value or "").strip().lower()
    m = SIZE_PATTERN.search(text)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if is_cm_dimensions(a, b, text):
        return None
    return f"{a}x{b}"


def _validate_size_cm(value: str) -> str | None:
    text = (value or "").strip().lower().replace("cm", "").strip()
    m = SIZE_PATTERN.search(text)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if not is_cm_dimensions(a, b, text + "cm"):
        return None
    return f"{a}x{b}cm"


def _price_dict_to_mongo_filter(price: dict | None) -> dict | None:
    """Convert internal validated price dict to apply_price_filter format."""
    if not price:
        return None
    currency = price.get("currency", "INR")
    op = price.get("operator")
    if op == "range":
        return {
            "currency": currency,
            "min_amount": price["min_amount"],
            "max_amount": price["max_amount"],
        }
    mongo_op = "$gte" if op == "gte" else "$lte"
    return {
        "currency": currency,
        "amount": price["amount"],
        "operator": mongo_op,
    }


def _parse_flat_price_from_raw(
    raw: dict,
    *,
    default_currency: str = "INR",
) -> dict | None:
    """Parse v2 flat price fields from LLM JSON."""
    if not raw.get("has_price_filter") or raw.get("price_type") == "none":
        return None

    currency = normalize_currency_code(raw.get("price_currency") or default_currency)
    if currency not in CURRENCY_FIELDS:
        return None

    price_type = (raw.get("price_type") or "none").lower()
    if price_type == "range":
        try:
            min_a = float(raw.get("price_min") or 0)
            max_a = float(raw.get("price_max") or 0)
            if min_a > 0 and max_a > 0:
                return {
                    "currency": currency,
                    "operator": "range",
                    "min_amount": min(min_a, max_a),
                    "max_amount": max(min_a, max_a),
                }
        except (TypeError, ValueError):
            return None
        return None

    if price_type not in ("gte", "lte"):
        return None
    try:
        amount = float(raw.get("price_amount") or 0)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return {"currency": currency, "operator": price_type, "amount": amount}


def _source_has_price_intent(source: str) -> bool:
    lower = (source or "").lower()
    price_words = (
        "above", "over", "under", "below", "budget", "between",
        "price", "cost", "inr", "usd", "eur", "gbp", "aed",
        "rupee", "dollar", "lakh", "lac", " crore", "k ",
    )
    if any(w in lower for w in price_words):
        return True
    return bool(re.search(r"\d\s*k\b", lower))


def reconcile_price_with_source(
    llm_price: dict | None,
    source_text: str,
    *,
    default_currency: str = "INR",
) -> tuple[dict | None, list[str]]:
    """Validate LLM price against source text; correct obvious hallucinations."""
    notes: list[str] = []
    source = (source_text or "").strip()
    if not source:
        return llm_price, notes

    anchor_pf = None
    if _source_has_price_intent(source):
        anchor_pf, _ = extract_price_filter_from_text(source.lower())

    if not llm_price:
        if anchor_pf:
            notes.append("price:filled_from_source_anchor")
            return _mongo_filter_to_internal_price(anchor_pf), notes
        return None, notes

    if not anchor_pf:
        return llm_price, notes

    if "amount" in llm_price and "amount" in anchor_pf:
        llm_amt = float(llm_price["amount"])
        anch_amt = float(anchor_pf["amount"])
        if anch_amt > 0 and (llm_amt < anch_amt * 0.5 or llm_amt > anch_amt * 2):
            notes.append(f"price:corrected_{int(llm_amt)}_to_{int(anch_amt)}")
            return _mongo_filter_to_internal_price(anchor_pf), notes

    if "min_amount" in llm_price and "min_amount" in anchor_pf:
        return llm_price, notes

    return llm_price, notes


def _mongo_filter_to_internal_price(pf: dict) -> dict:
    currency = pf.get("currency", "INR")
    if "min_amount" in pf and "max_amount" in pf:
        return {
            "currency": currency,
            "operator": "range",
            "min_amount": pf["min_amount"],
            "max_amount": pf["max_amount"],
        }
    op = pf.get("operator", "$lte")
    internal_op = "gte" if op == "$gte" else "lte"
    return {
        "currency": currency,
        "operator": internal_op,
        "amount": float(pf["amount"]),
    }


def build_search_payload_from_attrs(attrs: dict) -> dict[str, Any]:
    """Build Mongo search payload from validated LLM attrs — no regex normalise_keyword."""
    color_check_terms: set[str] = set()
    exact_color_terms: set[str] = set()
    attribute_filters: dict[str, set] = {
        "color": set(), "color_exact": set(), "shape": set(), "size": set(), "size_cm": set(),
        "size_category": set(),
        "material": set(), "construction": set(), "pattern": set(),
        "room": set(), "weight_max": set(), "multicolor": set(),
        "catalog_tag": set(),
    }

    for color in attrs.get("colors") or []:
        terms = color_search_terms(color)
        color_check_terms.update(terms)
        exact_color_terms.add(color)
        attribute_filters["color"].update(terms)
        attribute_filters["color_exact"].add(color)

    for shape in attrs.get("shapes") or []:
        attribute_filters["shape"].add(shape.lower())
    for size in attrs.get("sizes_ft") or []:
        attribute_filters["size"].add(size.lower())
    for size in attrs.get("sizes_cm") or []:
        attribute_filters["size_cm"].add(size.lower())
    for category in attrs.get("size_categories") or []:
        attribute_filters["size_category"].add(category.lower())
    for material in attrs.get("materials") or []:
        attribute_filters["material"].add(material)
    for construction in attrs.get("constructions") or []:
        attribute_filters["construction"].add(construction)
    for pattern in attrs.get("patterns") or []:
        attribute_filters["pattern"].add(pattern.lower())
    for room in attrs.get("rooms") or []:
        attribute_filters["room"].add(room)
    for tag in attrs.get("catalog_tags") or []:
        attribute_filters["catalog_tag"].add(tag)
    if attrs.get("multicolor"):
        attribute_filters["multicolor"].add("multicolor")
    weight = attrs.get("weight_max_kg")
    if weight:
        attribute_filters["weight_max"].add(float(weight))

    attribute_filters["color_exact"] = exact_color_terms
    price_filter = _price_dict_to_mongo_filter(attrs.get("price"))
    clean_keyword = attributes_to_catalog_keyword(attrs)

    return {
        "clean_keyword": clean_keyword,
        "price_filter": price_filter,
        "color_check_terms": color_check_terms,
        "attribute_filters": attribute_filters,
    }


def validate_extracted_attributes(
    raw: dict,
    *,
    source_text: str = "",
    default_currency: str = "INR",
    llm_primary: bool = False,
) -> tuple[dict, list[str]]:
    """Map LLM output to canonical catalog keys. Returns (validated, dropped_notes)."""
    dropped: list[str] = []
    out: dict[str, Any] = {
        "colors": [],
        "shapes": [],
        "sizes_ft": [],
        "sizes_cm": [],
        "size_categories": [],
        "materials": [],
        "constructions": [],
        "patterns": [],
        "rooms": [],
        "catalog_tags": [],
        "multicolor": bool(raw.get("multicolor")),
        "weight_max_kg": None,
        "price": None,
        "sku": None,
        "collection": None,
        "refinement": raw.get("refinement"),
    }

    for c in raw.get("colors") or []:
        key = _COLOR_KEYS_LOWER.get((c or "").strip().lower())
        if key and key not in out["colors"]:
            out["colors"].append(key)
        elif c:
            dropped.append(f"color:{c}")

    for s in raw.get("shapes") or []:
        key = _SHAPE_KEYS_LOWER.get((s or "").strip().lower())
        if key and key not in out["shapes"]:
            out["shapes"].append(key)
        elif s:
            dropped.append(f"shape:{s}")

    for p in raw.get("patterns") or []:
        key = _PATTERN_KEYS_LOWER.get((p or "").strip().lower())
        if key and key not in out["patterns"]:
            out["patterns"].append(key)
        elif p:
            dropped.append(f"pattern:{p}")

    for m in raw.get("materials") or []:
        matched = _match_material(m)
        if matched and matched not in out["materials"]:
            out["materials"].append(matched)
        elif m:
            dropped.append(f"material:{m}")

    for c in raw.get("constructions") or []:
        matched = _match_construction(c)
        if matched and matched not in out["constructions"]:
            out["constructions"].append(matched)
        elif c:
            dropped.append(f"construction:{c}")

    for r in raw.get("rooms") or []:
        # Outdoor is a ProductTag facet, not a Room value.
        tag = _match_catalog_tag(r)
        if tag == "outdoor":
            if tag not in out["catalog_tags"]:
                out["catalog_tags"].append(tag)
            continue
        matched = _match_room(r)
        if matched and matched not in out["rooms"]:
            out["rooms"].append(matched)
        elif r:
            dropped.append(f"room:{r}")

    for t in raw.get("catalog_tags") or []:
        matched = _match_catalog_tag(t)
        if matched and matched not in out["catalog_tags"]:
            out["catalog_tags"].append(matched)
        elif t:
            dropped.append(f"catalog_tag:{t}")

    for s in raw.get("sizes_ft") or []:
        lower = (s or "").strip().lower()
        canonical = normalise_size_category(lower)
        if canonical:
            if canonical not in out["size_categories"]:
                out["size_categories"].append(canonical)
            continue
        validated = _validate_size_ft(s)
        if validated and validated not in out["sizes_ft"]:
            out["sizes_ft"].append(validated)
        elif s:
            dropped.append(f"size_ft:{s}")

    for s in raw.get("size_categories") or []:
        lower = (s or "").strip().lower()
        canonical = normalise_size_category(lower)
        if canonical and canonical not in out["size_categories"]:
            out["size_categories"].append(canonical)
        elif s and not canonical:
            dropped.append(f"size_category:{s}")

    for s in raw.get("sizes_cm") or []:
        validated = _validate_size_cm(s)
        if validated and validated not in out["sizes_cm"]:
            out["sizes_cm"].append(validated)
        elif s:
            dropped.append(f"size_cm:{s}")

    weight = raw.get("weight_max_kg")
    if weight is not None:
        try:
            w = float(weight)
            if w > 0:
                out["weight_max_kg"] = w
        except (TypeError, ValueError):
            dropped.append(f"weight:{weight}")

    llm_price = _parse_flat_price_from_raw(raw, default_currency=default_currency)
    if llm_price:
        reconciled, price_notes = reconcile_price_with_source(
            llm_price,
            source_text,
            default_currency=default_currency,
        )
        dropped.extend(price_notes)
        out["price"] = reconciled
    elif raw.get("has_price_filter") and _source_has_price_intent(source_text):
        anchor_pf, _ = extract_price_filter_from_text(source_text.lower())
        if anchor_pf:
            out["price"] = _mongo_filter_to_internal_price(anchor_pf)
            dropped.append("price:llm_missing_used_anchor")

    sku = (raw.get("sku") or "").strip()
    if sku:
        out["sku"] = sku

    collection = (raw.get("collection") or "").strip()
    if collection:
        out["collection"] = collection

    return out, dropped


def attributes_to_catalog_keyword(attrs: dict) -> str:
    """Catalog-only &-joined keyword (no price — price uses price_filter directly)."""
    segments: list[str] = []

    for color in attrs.get("colors") or []:
        segments.append(color)
    for shape in attrs.get("shapes") or []:
        segments.append(shape)
    for size in attrs.get("sizes_ft") or []:
        segments.append(size)
    for size in attrs.get("sizes_cm") or []:
        segments.append(size)
    for material in attrs.get("materials") or []:
        segments.append(material)
    for construction in attrs.get("constructions") or []:
        segments.append(construction)
    for pattern in attrs.get("patterns") or []:
        segments.append(pattern)
    for room in attrs.get("rooms") or []:
        segments.append(room)
    for tag in attrs.get("catalog_tags") or []:
        segments.append(tag)
    # size_categories stay in attribute_filters only (sqft / SizeGroup post-filter).
    if attrs.get("multicolor"):
        segments.append("multicolor")
    weight = attrs.get("weight_max_kg")
    if weight:
        kg = int(weight) if float(weight).is_integer() else weight
        segments.append(f"{kg}kg")
    if attrs.get("sku"):
        segments.append(attrs["sku"])
    if attrs.get("collection"):
        segments.append(attrs["collection"])

    return "&".join(segments)


def attributes_to_keyword_string(attrs: dict) -> str:
    """Convert validated attributes to &-joined keyword (includes price for hybrid/regex paths)."""
    segments = [s for s in attributes_to_catalog_keyword(attrs).split("&") if s]

    price = attrs.get("price")
    if isinstance(price, dict):
        currency = price.get("currency", "INR")
        if price.get("operator") == "range":
            segments.append(
                f"between {currency} {int(price['min_amount'])} to {currency} {int(price['max_amount'])}"
            )
        elif price.get("operator") == "gte":
            segments.append(f"above {currency} {int(price['amount'])}")
        elif price.get("operator") == "lte":
            segments.append(f"under {currency} {int(price['amount'])}")

    return "&".join(segments)


def _has_price_segment(keyword: str) -> bool:
    price_filter, _, _, _ = normalise_keyword(keyword)
    return price_filter is not None


def merge_extraction_keywords(
    base_keyword: str,
    llm_keyword: str,
    *,
    prefer_llm_price: bool = False,
) -> str:
    """Union segments from base and LLM keywords.

    Default: regex/base price wins on conflict.
    llm_primary: LLM price wins when both have a price segment.
    """
    base = (base_keyword or "").strip()
    llm = (llm_keyword or "").strip()
    if not llm:
        return base
    if not base:
        return llm

    base_has_price = _has_price_segment(base)
    llm_has_price = _has_price_segment(llm)

    base_parts = [p.strip() for p in base.split("&") if p.strip()]
    llm_parts = [p.strip() for p in llm.split("&") if p.strip()]

    if base_has_price and llm_has_price:
        if prefer_llm_price:
            base_parts = [p for p in base_parts if not _has_price_segment(p)]
        else:
            llm_parts = [p for p in llm_parts if not _has_price_segment(p)]

    seen_lower: set[str] = set()
    merged: list[str] = []
    order = llm_parts + base_parts if prefer_llm_price else base_parts + llm_parts
    for part in order:
        key = part.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        merged.append(part)

    return "&".join(merged)


def _catalog_hints() -> str:
    # Full lists — truncating colors previously dropped purple/violet/etc.
    colors = ", ".join(sorted(COLOR_ALIASES.keys()))
    shapes = ", ".join(sorted(SHAPE_ALIASES.keys()))
    patterns = ", ".join(sorted(PATTERN_ALIASES.keys()))
    materials = ", ".join(MATERIAL_KEYWORDS)
    tags = ", ".join(sorted(CATALOG_TAG_KEYS))
    tag_aliases = ", ".join(sorted(CATALOG_TAG_ALIASES.keys()))
    return (
        f"Allowed color keys (use EXACTLY these strings in colors[]): {colors}\n"
        f"Allowed shape keys: {shapes}\n"
        f"Allowed pattern/style keys: {patterns}\n"
        f"Allowed materials: {materials}\n"
        f"Allowed constructions: {', '.join(CONSTRUCTION_KEYWORDS)}\n"
        f"Allowed rooms: {', '.join(ROOM_KEYWORDS)}\n"
        f"Allowed catalog_tags (use EXACTLY these): {tags}\n"
        f"catalog_tags phrase aliases: {tag_aliases}"
    )


def build_extraction_prompt() -> str:
    return f"""You extract product-search attributes for Jaipur Rugs. Output MUST follow the JSON schema.

MISSION
Extract EVERY concrete shopping attribute the user (or tool keyword) asked for.
Missing a stated color/size/material is a critical failure. Inventing attributes is also a failure.

SOURCE PRIORITY (read ALL of them; merge attributes from every source):
1) User message (any language / typos / slang)
2) Agent tool keyword if present (often already structured like purple&12x15 — TRUST it)
3) Previous search keyword + recent chat only for refinement/show_more context

NEVER-DROP RULES
- If a color word appears in the user message OR tool keyword, it MUST appear in colors[].
  Examples that MUST yield colors=["purple"]: "purple rugs", "show purple", "बैंगनी", "baingani", "purple&12x15".
- If a foot size appears (8x10, 8×10, 8*10, 8 by 10, 12x15), put it in sizes_ft as NxM (ascii x).
  "in range of size 12*15" / "size 12x15" / "12 by 15" → sizes_ft=["12x15"].
- If both color AND size are present, extract BOTH. Never keep only size.
- Typos: rungs→rugs (ignore), purpel→purple, gry→grey, woollen→wool when clear.
- Multilingual: map common color words to English catalog keys
  (e.g. baingani/बैंगनी→purple, neela/नीला→blue, laal/लाल→red, safed→white, kala→black).
- Do NOT treat "range of size" as a price range. Size language ≠ price.

SIZE RULES
- Foot dimensions → sizes_ft (normalize separators * × by / - to x). Never put 8x10 in size_categories.
- "6 dia round" / "8' round" → sizes_ft=["6 dia round"] or ["8 round"] AND shapes=["round"].
- size_categories only for bucket words: small, medium, large, oversize (map oversized→oversize).
- CM dimensions (e.g. 240x300 cm, 240×300cm) → sizes_cm.

CATALOG TAG RULES (website mega-menu)
- new arrival / new arrivals → catalog_tags=["new"]
- bestsellers / best seller → catalog_tags=["bestseller"]
- antique rugs → catalog_tags=["antique"]
- rug swatch / swatches → catalog_tags=["swatch"]
- outdoor rugs → catalog_tags=["outdoor"] (NOT rooms — Outdoor is a product tag)
- Fresh mega-menu browse (e.g. only "new arrival rugs" / "bestsellers"): set refinement="new"
  and do NOT copy colors/sizes/materials/rooms from PREVIOUS SEARCH KEYWORD.
  Previous search is for refine_previous / show_more only (also / same / but in blue / show more).

PRICE RULES
- Any budget/price/cost/above/under/over/below/between/k/lakh/lac/cr → has_price_filter=true.
- Expand shorthand to full integers: 50k→50000, 1.5k→1500, 2 lakh/2lac/2l→200000, 4lc→400000, 1cr→10000000.
- price_type: gte | lte | range | none. price_currency: INR|USD|EUR|GBP|AUD|CHF|SGD|AED.
- price_raw_phrase = exact price words from the user. Never invent amounts.

CATALOG RULES
- colors/shapes/patterns/materials/constructions/rooms/catalog_tags MUST use keys from the allowed lists below.
- Empty array only when that attribute was NOT mentioned. Do not guess.
- refinement: refine_previous | show_more | new.

EXAMPLES (attribute intent only):
"show me above 50k usd"
→ has_price_filter=true, price_type=gte, price_currency=USD, price_amount=50000

"blue round rugs under INR 50000"
→ colors=["blue"], shapes=["round"], has_price_filter=true, price_type=lte, price_currency=INR, price_amount=50000

"show me purple rugs in range of size 12*15"
→ colors=["purple"], sizes_ft=["12x15"]   ← BOTH required; "range of size" is NOT price

"purple&12x15" (tool keyword)
→ colors=["purple"], sizes_ft=["12x15"]

"show me red wool 8x10"
→ colors=["red"], materials=["wool"], sizes_ft=["8x10"]

"red medium size rug above 10 lac for bedroom"
→ colors=["red"], size_categories=["medium"], rooms=["bedroom"], has_price_filter=true, price_type=gte, price_currency=INR, price_amount=1000000

"baingani gol dari 8x10"
→ colors=["purple"], shapes=["round"], sizes_ft=["8x10"]

"hand tufted blue round under 2 lakh"
→ colors=["blue"], shapes=["round"], constructions=["hand tufted"], has_price_filter=true, price_type=lte, price_currency=INR, price_amount=200000

"show me new arrival rugs"
→ catalog_tags=["new"]

"bestsellers"
→ catalog_tags=["bestseller"]

"outdoor rugs"
→ catalog_tags=["outdoor"]

"antique rugs"
→ catalog_tags=["antique"]

"rug swatch"
→ catalog_tags=["swatch"]

{_catalog_hints()}"""


def backfill_attrs_from_regex(
    attrs: dict,
    *,
    keyword: str = "",
    user_message: str = "",
) -> tuple[dict, list[str]]:
    """Fill gaps the LLM missed using deterministic regex/alias parsing.

    Never removes LLM values — only adds missing colors/sizes/shapes/etc.
    Parses keyword and user_message separately (joining them confuses the parser).
    """
    notes: list[str] = []
    sources = [s for s in ((keyword or "").strip(), (user_message or "").strip()) if s]
    if not sources:
        return attrs, notes

    color_terms: set[str] = set()
    filters: dict[str, set] = {
        "color": set(),
        "color_exact": set(),
        "shape": set(),
        "size": set(),
        "size_cm": set(),
        "size_category": set(),
        "material": set(),
        "construction": set(),
        "pattern": set(),
        "room": set(),
        "weight_max": set(),
        "multicolor": set(),
        "catalog_tag": set(),
    }
    for source in sources:
        _pf, _clean, terms, parsed = normalise_keyword(source)
        color_terms.update(terms or set())
        for key, values in (parsed or {}).items():
            filters.setdefault(key, set()).update(values or set())

    out = dict(attrs)
    for key in (
        "colors", "shapes", "sizes_ft", "sizes_cm", "size_categories",
        "materials", "constructions", "patterns", "rooms", "catalog_tags",
    ):
        out[key] = list(out.get(key) or [])

    for color in sorted(filters.get("color_exact") or set()):
        if color not in out["colors"]:
            out["colors"].append(color)
            notes.append(f"backfill:color:{color}")

    if not out["colors"] and color_terms:
        for term in sorted(color_terms):
            key = _COLOR_KEYS_LOWER.get(term.lower())
            if key and key not in out["colors"]:
                out["colors"].append(key)
                notes.append(f"backfill:color_term:{key}")

    for shape in sorted(filters.get("shape") or set()):
        if shape not in out["shapes"]:
            out["shapes"].append(shape)
            notes.append(f"backfill:shape:{shape}")

    for size in sorted(filters.get("size") or set()):
        if size not in out["sizes_ft"]:
            out["sizes_ft"].append(size)
            notes.append(f"backfill:size:{size}")

    for size in sorted(filters.get("size_cm") or set()):
        if size not in out["sizes_cm"]:
            out["sizes_cm"].append(size)
            notes.append(f"backfill:size_cm:{size}")

    for category in sorted(filters.get("size_category") or set()):
        if category not in out["size_categories"]:
            out["size_categories"].append(category)
            notes.append(f"backfill:size_category:{category}")

    for material in sorted(filters.get("material") or set()):
        if material not in out["materials"]:
            out["materials"].append(material)
            notes.append(f"backfill:material:{material}")

    for construction in sorted(filters.get("construction") or set()):
        if construction not in out["constructions"]:
            out["constructions"].append(construction)
            notes.append(f"backfill:construction:{construction}")

    for pattern in sorted(filters.get("pattern") or set()):
        if pattern not in out["patterns"]:
            out["patterns"].append(pattern)
            notes.append(f"backfill:pattern:{pattern}")

    for room in sorted(filters.get("room") or set()):
        if room not in out["rooms"]:
            out["rooms"].append(room)
            notes.append(f"backfill:room:{room}")

    for tag in sorted(filters.get("catalog_tag") or set()):
        if tag not in out["catalog_tags"]:
            out["catalog_tags"].append(tag)
            notes.append(f"backfill:catalog_tag:{tag}")

    if (filters.get("multicolor") or set()) and not out.get("multicolor"):
        out["multicolor"] = True
        notes.append("backfill:multicolor")

    if notes:
        logger.info(f"[LLM-EXTRACT] regex backfill applied: {notes}")
    return out, notes


async def extract_search_attributes(
    user_message: str,
    *,
    keyword: str = "",
    previous_search_keyword: str = "",
    chat_context: str = "",
    detected_currency: str = "",
    country_code: str = "",
    llm_primary: bool = False,
) -> tuple[dict, list[str]]:
    """Call LLM to extract attributes. Returns (validated_attrs, dropped_notes)."""
    if not _client:
        logger.warning("[LLM-EXTRACT] OPENAI_API_KEY not configured — skipping")
        return {}, ["no_api_key"]

    query_text = (user_message or keyword or "").strip()
    if not query_text:
        return {}, ["empty_query"]

    default_currency = normalize_currency_code(detected_currency or "INR")
    tool_kw = (keyword or "").strip()

    user_content = (
        "Extract ALL search attributes from the sources below. "
        "If a color or size appears in ANY source, you MUST include it.\n\n"
        f"USER MESSAGE:\n{query_text}\n\n"
        f"Default currency if not specified: {default_currency}\n"
        f"User country code: {country_code or 'unknown'}"
    )
    if tool_kw and tool_kw.lower() != query_text.lower():
        user_content += (
            f"\n\nAGENT TOOL KEYWORD (first-class source — parse every & segment):\n{tool_kw}"
        )
    if previous_search_keyword:
        user_content += (
            f"\n\nPREVIOUS SEARCH KEYWORD (refinement context only):\n{previous_search_keyword}"
        )
    if chat_context:
        user_content += f"\n\nRECENT CONVERSATION:\n{chat_context}"

    try:
        response = await _client.responses.create(
            model="gpt-4.1-mini",
            instructions=build_extraction_prompt(),
            input=[{"role": "user", "content": user_content}],
            temperature=0,
            max_output_tokens=768,
            text=SEARCH_EXTRACTION_SCHEMA,
        )
        raw_text = response.output_text
        raw = json.loads(raw_text) if raw_text else {}
        if not isinstance(raw, dict):
            return {}, ["invalid_json_type"]
        logger.info(f"[LLM-EXTRACT] raw LLM JSON: {raw}")
        attrs, dropped = validate_extracted_attributes(
            raw,
            source_text=query_text,
            default_currency=default_currency,
            llm_primary=llm_primary,
        )
        attrs, backfill_notes = backfill_attrs_from_regex(
            attrs,
            keyword=tool_kw,
            user_message=query_text,
        )
        dropped.extend(backfill_notes)
        return attrs, dropped
    except Exception as err:
        logger.warning(f"[LLM-EXTRACT] extraction failed: {err}")
        return {}, [f"error:{err}"]


def _attrs_have_searchable_content(attrs: dict) -> bool:
    if attrs.get("multicolor"):
        return True
    if attrs.get("weight_max_kg"):
        return True
    if attrs.get("price"):
        return True
    if attrs.get("sku") or attrs.get("collection"):
        return True
    for key in (
        "colors", "shapes", "sizes_ft", "sizes_cm", "size_categories",
        "materials", "constructions", "patterns", "rooms", "catalog_tags",
    ):
        if attrs.get(key):
            return True
    return False


async def resolve_keyword_with_llm_extraction(
    keyword: str,
    *,
    user_message: str = "",
    previous_search_keyword: str = "",
    chat_context: str = "",
    skip_llm_extraction: bool = False,
    extraction_mode: str | None = None,
    detected_currency: str = "",
    country_code: str = "",
) -> tuple[str, dict | None]:
    """Enrich keyword via LLM per SEARCH_EXTRACTION_MODE.

    Returns (resolved_keyword, debug_info_or_none).
    In llm_primary mode, debug includes search_payload that bypasses regex normalise_keyword.
    """
    mode = extraction_mode or get_search_extraction_mode()
    kw = (keyword or "").strip()
    source = _source_text_for_extraction(kw, user_message)
    if not kw and user_message:
        kw = user_message.strip()

    run_llm, reason = should_run_llm_extraction(
        kw,
        user_message=user_message,
        skip_llm_extraction=skip_llm_extraction,
        extraction_mode=mode,
    )
    if not run_llm:
        logger.info(f"[LLM-EXTRACT] skip mode={mode} reason={reason} keyword={kw!r}")
        return kw, {"ran": False, "mode": mode, "reason": reason}

    # Detect from the human message first — tool keyword may still say "new&…"
    ignore_previous = _should_ignore_previous_search(
        "",
        user_message or source,
    ) or _should_ignore_previous_search(kw, user_message or source)
    prev_for_llm = "" if ignore_previous else previous_search_keyword
    context_for_llm = "" if ignore_previous else chat_context
    extract_kw = kw
    extract_source = source
    if ignore_previous:
        # Drop polluted agent tool keyword (e.g. prior "new" + price).
        extract_source = (user_message or source or kw).strip()
        extract_kw = extract_source
        if kw and kw.lower() != extract_kw.lower():
            logger.info(
                f"[LLM-EXTRACT] replacing polluted tool keyword={kw!r} "
                f"with user_message={extract_kw!r}"
            )
        if previous_search_keyword:
            logger.info(
                f"[LLM-EXTRACT] ignoring previous_search for fresh catalog_tag browse "
                f"prev={previous_search_keyword!r}"
            )

    logger.info(
        f"[LLM-EXTRACT] run mode={mode} reason={reason} "
        f"keyword_in={extract_kw!r} source={extract_source!r}"
    )
    attrs, dropped = await extract_search_attributes(
        extract_source,
        keyword=extract_kw if extract_kw.lower() != extract_source.lower() else "",
        previous_search_keyword=prev_for_llm,
        chat_context=context_for_llm,
        detected_currency=detected_currency,
        country_code=country_code,
        llm_primary=(mode == "llm_primary"),
    )

    if ignore_previous:
        attrs = _sanitize_attrs_to_current_message(
            attrs,
            keyword="",
            user_message=extract_source,
        )
        dropped = list(dropped) + ["sanitized:fresh_catalog_tag_browse"]

    if not _attrs_have_searchable_content(attrs):
        logger.warning(f"[LLM-EXTRACT] no searchable content after validation dropped={dropped}")
        return kw, {
            "ran": True,
            "mode": mode,
            "reason": reason,
            "merged_keyword": kw,
            "dropped_fields": dropped,
            "empty": True,
        }

    if mode == "llm_primary":
        payload = build_search_payload_from_attrs(attrs)
        catalog_kw = payload["clean_keyword"]
        logger.info(
            f"[LLM-EXTRACT] llm_primary payload price_filter={payload['price_filter']} "
            f"clean_keyword={catalog_kw!r} dropped={dropped}"
        )
        return catalog_kw, {
            "ran": True,
            "mode": mode,
            "reason": reason,
            "use_llm_payload": True,
            "search_payload": payload,
            "merged_keyword": catalog_kw,
            "dropped_fields": dropped,
            "attributes": attrs,
            "llm_raw_price": attrs.get("price"),
            "ignored_previous_search": ignore_previous,
        }

    prefer_llm_price = (
        mode == "hybrid"
        and reason == "weak_or_suspicious_price"
        and bool(attrs.get("price"))
    )
    llm_keyword = attributes_to_keyword_string(attrs)
    merged = merge_extraction_keywords(kw, llm_keyword, prefer_llm_price=prefer_llm_price)
    logger.info(
        f"[LLM-EXTRACT] mode={mode} llm_out={llm_keyword!r} merged={merged!r} dropped={dropped}"
    )
    return merged, {
        "ran": True,
        "mode": mode,
        "reason": reason,
        "use_llm_payload": False,
        "llm_keyword": llm_keyword,
        "merged_keyword": merged,
        "dropped_fields": dropped,
        "attributes": attrs,
    }
