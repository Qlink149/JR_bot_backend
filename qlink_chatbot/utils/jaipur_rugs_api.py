import random
import re
from datetime import datetime

import httpx

from qlink_chatbot.database.mongo_utils import db
from qlink_chatbot.utils.jr_api_client import search_products as _jr_search_products
from qlink_chatbot.utils.logger_config import logger

# Kept for dashboard_routes imports
products_collection = db["products"]
product_color_collection = db["product_color"]
search_cache_collection = db["product_search_cache"]

# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

CURRENCY_FIELDS = {
    "INR": "INR_MRP", "USD": "USD_MRP", "EUR": "EUR_MRP",
    "GBP": "GBP_MRP", "AUD": "AUD_MRP", "CHF": "CHF_MRP",
    "SGD": "SGD_MRP", "AED": "AED_MRP",
}

CURRENCY_ALIASES = {
    "inr": "INR", "rs": "INR", "rupee": "INR", "rupees": "INR",
    "usd": "USD", "dollar": "USD", "dollars": "USD",
    "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
    "aud": "AUD", "chf": "CHF", "sgd": "SGD", "aed": "AED",
}

CALLING_CODE_TO_CURRENCY: dict[str, str] = {
    "91": "INR", "971": "AED", "966": "AED", "965": "AED",
    "974": "AED", "968": "AED", "973": "AED", "61": "AUD",
    "41": "CHF", "423": "CHF", "44": "GBP", "65": "SGD",
    "1": "USD", "49": "EUR", "33": "EUR", "39": "EUR",
    "34": "EUR", "31": "EUR", "32": "EUR", "43": "EUR",
    "351": "EUR", "30": "EUR", "358": "EUR", "353": "EUR",
    "352": "EUR", "356": "EUR", "386": "EUR", "421": "EUR",
    "372": "EUR", "371": "EUR", "370": "EUR", "357": "EUR",
}
DEFAULT_CURRENCY = "INR"

PRICE_OPERATOR_WORDS = {
    "above": "$gte", "over": "$gte", "more than": "$gte",
    "greater than": "$gte", "higher than": "$gte",
    "minimum": "$gte", "min": "$gte", "from": "$gte",
    "starting from": "$gte", "at least": "$gte",
    "below": "$lte", "under": "$lte", "less than": "$lte",
    "lower than": "$lte", "maximum": "$lte", "max": "$lte",
    "up to": "$lte", "upto": "$lte", "within": "$lte",
    "budget": "$lte", "for": "$lte",
}

AMOUNT_MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000,
    "l": 100_000, "lac": 100_000, "lacs": 100_000,
    "lakh": 100_000, "lakhs": 100_000, "lc": 100_000,
    "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000,
    "m": 1_000_000, "million": 1_000_000,
}

PRICE_CONTEXT_WORDS = {
    "price", "priced", "cost", "costing", "amount", "budget", "range",
    "mrp", "rs", "rupee", "rupees", "inr", "usd", "eur", "gbp", "aud",
    "chf", "sgd", "aed", "dollar", "dollars", "euro", "euros", "pound", "pounds",
}

# ---------------------------------------------------------------------------
# Currency helpers
# ---------------------------------------------------------------------------

def _normalize_currency_code(value: str) -> str:
    normalized = (value or DEFAULT_CURRENCY).lower().strip()
    return CURRENCY_ALIASES.get(normalized, normalized.upper())


def _format_price_amount(value) -> str:
    if value is None or value == "":
        return ""
    try:
        amount = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return str(value).strip()
    if amount.is_integer():
        return f"{int(amount):,}"
    return f"{amount:,.2f}".rstrip("0").rstrip(".")


def _build_display_price(currency: str, amount) -> str:
    formatted = _format_price_amount(amount)
    return f"{currency} {formatted}" if formatted else ""


def _currency_alias_pattern() -> str:
    return "|".join(
        re.escape(a) for a in sorted(CURRENCY_ALIASES, key=len, reverse=True)
    )


def _extract_requested_currency_from_text(text: str) -> str:
    lower = (text or "").lower()
    alias_pattern = _currency_alias_pattern()
    for pattern in [
        rf"\bin\s*({alias_pattern})\b",
        rf"\bin({alias_pattern})\b",
        rf"\b({alias_pattern})\s*(?:price|prices|pricing|mrp)\b",
        rf"\b(?:price|prices|pricing|mrp)\s*(?:in\s*)?({alias_pattern})\b",
    ]:
        m = re.search(pattern, lower)
        if m:
            c = _normalize_currency_code(m.group(1))
            if c in CURRENCY_FIELDS:
                return c
    return ""


def _resolve_currency_from_country_code(country_code: str) -> str:
    digits = re.sub(r"\D", "", country_code or "")
    for length in range(min(3, len(digits)), 0, -1):
        c = CALLING_CODE_TO_CURRENCY.get(digits[:length])
        if c:
            return c
    return ""


async def _resolve_currency_from_ip(ip: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"http://ip-api.com/json/{ip}?fields=currency,status")
            data = resp.json()
            if data.get("status") == "success":
                c = data.get("currency", "").upper()
                if c in CURRENCY_FIELDS:
                    logger.info(f"[CURRENCY] resolved {c} from IP {ip}")
                    return c
    except Exception as e:
        logger.warning(f"[CURRENCY] IP geolocation failed for {ip}: {e}")
    return DEFAULT_CURRENCY

# ---------------------------------------------------------------------------
# Price extraction from keyword (strips price from keyword sent to JR API)
# ---------------------------------------------------------------------------

def _parse_amount_with_suffix(amount_text: str, suffix: str = "") -> float:
    amount = float(amount_text.replace(",", ""))
    return amount * AMOUNT_MULTIPLIERS.get((suffix or "").lower(), 1)


def _is_probable_price_amount(amount: float, suffix: str, context: str) -> bool:
    if suffix:
        return True
    if amount >= 1000:
        return True
    return any(w in context.split() for w in PRICE_CONTEXT_WORDS)


def _extract_price_filter_from_text(text: str) -> tuple[dict | None, str]:
    """Return (price_filter, cleaned_text_without_price)."""
    alias_pattern = _currency_alias_pattern()
    operators = "|".join(
        re.escape(w) for w in sorted(PRICE_OPERATOR_WORDS, key=len, reverse=True)
    )
    amount = r"([\d,]+(?:\.\d+)?)\s*(k|thousand|lacs?|lakhs?|lakh|lc|l|cr|crores?|m|million)?"

    range_match = re.search(
        rf"\bbetween\s+(?:({alias_pattern})\s*)?{amount}\s+"
        rf"(?:and|to|-)\s+(?:({alias_pattern})\s*)?{amount}",
        text,
    )
    if range_match:
        fc, min_t, min_s, sc, max_t, max_s = range_match.groups()
        min_a = _parse_amount_with_suffix(min_t, min_s or "")
        max_a = _parse_amount_with_suffix(max_t, max_s or min_s or "")
        currency = fc or sc or DEFAULT_CURRENCY
        return (
            {
                "currency": _normalize_currency_code(currency),
                "min_amount": min(min_a, max_a),
                "max_amount": max(min_a, max_a),
            },
            (text[:range_match.start()] + " " + text[range_match.end():]).strip(),
        )

    patterns = [
        rf"\b({operators})\b\s*{amount}\s*({alias_pattern})\b",
        rf"\b({operators})\b\s*(?:({alias_pattern})\s*)?{amount}",
        rf"\b(?:({alias_pattern})\s*)?{amount}\s*\b({operators})\b",
        rf"\b({alias_pattern})\s+{amount}\b",
        rf"\b{amount}\s*({alias_pattern})\b",
        rf"\b{amount}\b",
    ]
    for idx, pattern in enumerate(patterns):
        m = re.search(pattern, text)
        if not m:
            continue
        groups = m.groups()
        if idx == 0:
            op, at, sf, cur = groups
        elif idx == 1:
            op, cur, at, sf = groups
        elif idx == 2:
            cur, at, sf, op = groups
        elif idx == 3:
            cur, at, sf = groups; op = "budget"
        elif idx == 4:
            at, sf, cur = groups; op = "budget"
        else:
            at, sf = groups; cur = DEFAULT_CURRENCY; op = "budget"

        parsed = _parse_amount_with_suffix(at, sf or "")
        if not cur and not _is_probable_price_amount(
            parsed, sf or "", f"{text[:m.start()]} {text[m.end():]}"
        ):
            continue

        return (
            {
                "currency": _normalize_currency_code(cur or DEFAULT_CURRENCY),
                "amount": parsed,
                "operator": PRICE_OPERATOR_WORDS.get(op, "$lte"),
            },
            (text[:m.start()] + " " + text[m.end():]).strip(),
        )

    return None, text


def _apply_price_filter(products: list, price_filter: dict) -> list:
    if not price_filter:
        return products
    currency = price_filter.get("currency", "INR")
    currency_field = CURRENCY_FIELDS.get(currency, "INR_MRP")
    result = []
    for p in products:
        raw_price = p.get(currency_field)
        try:
            price = float(str(raw_price).replace(",", "")) if raw_price not in (None, "", "0") else 0.0
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        if "min_amount" in price_filter and price < price_filter["min_amount"]:
            continue
        if "max_amount" in price_filter and price > price_filter["max_amount"]:
            continue
        if "amount" in price_filter:
            amount = price_filter["amount"]
            op = price_filter.get("operator", "$lte")
            if op == "$lte" and price > amount:
                continue
            if op == "$gte" and price < amount:
                continue
            if op == "$lt" and price >= amount:
                continue
            if op == "$gt" and price <= amount:
                continue
        result.append(p)
    return result

# ---------------------------------------------------------------------------
# Keyword normalisation before sending to JR API
# ---------------------------------------------------------------------------

_SIZE_PATTERN = re.compile(
    r"\b(\d+)\s*(?:x|by|\*|X)\s*(\d+)\b", re.IGNORECASE
)
_WEIGHT_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s*kg\b", re.IGNORECASE)
_NOISE_WORDS = {
    "show", "me", "find", "search", "looking", "look", "need", "want",
    "please", "rug", "rugs", "carpet", "carpets", "in", "the", "a", "an",
    "and", "or", "with", "of", "for",
}


# Maps user color/pattern words → exact JR API catalog values (GrColor, ColorFamily, DisplayFilter)
# Using || so the API text-searches across all those field values simultaneously.
_COLOR_ALIASES: dict[str, str] = {
    # Reds / Oranges / Rusts
    "red":          "Red||Crimson||Rust||Scarlet||Maroon",
    "crimson":      "Crimson||Red||Scarlet",
    "rust":         "Rust||Copper||Copper Tan||Terracotta",
    "terracotta":   "Terracotta||Rust||Copper||Burnt Orange",
    "orange":       "Orange||Copper||Rust||Amber",
    "copper":       "Copper||Copper Tan||Rust",
    "maroon":       "Maroon||Crimson||Red",
    "burgundy":     "Burgundy||Crimson||Maroon",
    # Blues
    "blue":         "Blue||Navy||Teal",
    "navy":         "Navy||Blue||Indigo",
    "navy blue":    "Navy||Blue",
    "teal":         "Teal||Blue||Turquoise",
    "indigo":       "Indigo||Navy||Blue",
    # Greens
    "green":        "Green||Olive||Sage||Jade",
    "olive":        "Green||Olive||Sage||Moss",
    "sage":         "Green||Sage||Olive||Mint",
    "emerald":      "Green||Emerald||Jade",
    # Greys / Blacks
    "grey":         "Classic Gray||Charcoal||Slate||Gray",
    "gray":         "Classic Gray||Charcoal||Slate||Gray",
    "charcoal":     "Charcoal||Classic Gray||Slate",
    "silver":       "Classic Gray||Silver||Gray",
    "black":        "Black||Charcoal||Ebony",
    "dark":         "Charcoal||Dark||Black||Navy Blue",
    # Whites / Ivories / Creams
    "white":        "White||Ivory||Antique White||Cream",
    "ivory":        "Ivory||Antique White||Cream||White",
    "cream":        "Cream||Ivory||Antique White||White",
    "off-white":    "Ivory||Antique White||Cream||White",
    "off white":    "Ivory||Antique White||Cream||White",
    # Beiges / Browns / Sands
    "beige":        "Beige||Sand||Camel||Tan",
    "sand":         "Sand||Beige||Camel||Tan",
    "tan":          "Tan||Sand||Camel||Copper Tan",
    "taupe":        "Taupe||Beige||Sand",
    "brown":        "Brown||Chocolate||Walnut||Caramel",
    "chocolate":    "Chocolate||Brown||Walnut",
    # Golds / Yellows
    "gold":         "Gold||Golden||Mustard||Amber",
    "golden":       "Golden||Gold||Amber",
    "mustard":      "Mustard||Gold||Yellow",
    "yellow":       "Yellow||Mustard||Gold",
    # Pinks / Purples  — only JR catalog GrColor values (partial substring matches work)
    "pink":         "Pink||Blush||Rose||Mauve||Coral",
    "blush":        "Blush||Pink||Rose",
    "rose":         "Rose||Pink||Blush",
    "coral":        "Coral||Terracotta||Rust",
    "mauve":        "Mauve||Blush||Pink",
    "purple":       "Purple||Lavender||Violet||Plum||Wisteria||Amethyst",
    "lavender":     "Lavender||Purple||Lilac||Wisteria",
    "violet":       "Violet||Purple||Lavender",
    "wisteria":     "Wisteria||Lavender||Purple",
    # Multi
    "multicolor":   "Multi",
    "multi":        "Multi",
    "multi color":  "Multi",
    "multicolour":  "Multi",
    "colorful":     "Multi",
}

# Maps shape user words → exact JR API catalog Shape values
_SHAPE_ALIASES: dict[str, str] = {
    "round":        "Round",
    "circular":     "Round",
    "circle":       "Round",
    "oval":         "Oval",
    "square":       "Square",
    "runner":       "Runner",
    "rectangular":  "Rectangle",
    "rectangle":    "Rectangle",
    "irregular":    "Irregular",
}

# Maps pattern/style user words → exact JR API catalog values
_PATTERN_ALIASES: dict[str, str] = {
    "solid":        "Solid",
    "plain":        "Solid",
    "geometric":    "Geometric",
    "geo":          "Geometric",
    "floral":       "Floral",
    "abstract":     "Abstract",
    "tribal":       "Moroccan and Tribal",
    "moroccan":     "Moroccan and Tribal",
    "boho":         "Moroccan and Tribal",
    "bohemian":     "Moroccan and Tribal",
    "medallion":    "Medallion",
    "modern":       "Modern",
    "contemporary": "Contemporary",
    "traditional":  "Traditional",
    "classic":      "Traditional",
    "transitional": "Transitional",
    "vintage":      "Traditional",
}

_MATERIAL_KEYWORDS = (
    "wool and bamboo silk", "wool and viscose", "bamboo silk", "pure silk",
    "wool", "silk", "viscose", "cotton", "bamboo", "jute", "leather", "nylon",
)
_CONSTRUCTION_KEYWORDS = (
    "hand knotted", "hand tufted", "hand loom", "hand woven", "flat weave",
    "machine made", "handmade",
)
_ROOM_KEYWORDS = (
    "living room", "dining room", "bedroom", "outdoor", "bathroom",
    "kitchen", "hallway", "office", "kids room", "entryway",
)


def _expand_term(segment: str, *, multi_attribute: bool = False) -> str:
    """Expand a keyword segment for the JR API.

    JR API rules (verified against product-master-search):
    - Single attribute: color || expansion helps (e.g. pink → Pink||Blush||Rose||...)
    - Multi attribute (&): keep simple terms (e.g. blue&round). Color || expansion
      breaks AND and the second attribute is ignored.
    """
    if "||" in segment:
        expanded = []
        for part in segment.split("||"):
            key = part.strip().lower()
            if key in _SHAPE_ALIASES:
                expanded.append(_SHAPE_ALIASES[key])
            elif multi_attribute:
                expanded.append(part.strip())
            else:
                expanded.append(
                    _COLOR_ALIASES.get(key) or _PATTERN_ALIASES.get(key) or part.strip()
                )
        return "||".join(expanded)

    key = segment.strip().lower()
    if key in _SHAPE_ALIASES:
        return _SHAPE_ALIASES[key]
    if multi_attribute:
        if key in _PATTERN_ALIASES:
            return _PATTERN_ALIASES[key]
        return segment.strip()
    return _COLOR_ALIASES.get(key) or _PATTERN_ALIASES.get(key) or segment


def _preprocess_natural_language(keyword: str) -> str:
    """Convert natural language like 'show me blue round rugs' → 'blue&round'."""
    text = (keyword or "").lower().strip()
    if "&" in text:
        return keyword.strip()

    text = _SIZE_PATTERN.sub(lambda m: f"{m.group(1)}x{m.group(2)}", text)
    words = [w for w in re.split(r"[\s,]+", text) if w and w not in _NOISE_WORDS]
    if not words:
        return keyword.strip()

    found: list[str] = []
    remaining = " ".join(words)

    for kw in sorted(_CONSTRUCTION_KEYWORDS, key=len, reverse=True):
        if kw in remaining:
            found.append(kw)
            remaining = remaining.replace(kw, " ").strip()

    for kw in sorted(_MATERIAL_KEYWORDS, key=len, reverse=True):
        if kw in remaining:
            found.append(kw)
            remaining = remaining.replace(kw, " ").strip()

    for alias in sorted(_COLOR_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append(alias)
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()

    for alias in sorted(_SHAPE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append(alias)
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()

    for alias in sorted(_PATTERN_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append(alias)
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()

    for room in sorted(_ROOM_KEYWORDS, key=len, reverse=True):
        if room in remaining:
            found.append(room)
            remaining = remaining.replace(room, " ").strip()

    weight_match = _WEIGHT_PATTERN.search(remaining)
    if weight_match:
        found.append(weight_match.group(0).replace(" ", ""))
        remaining = remaining[:weight_match.start()] + remaining[weight_match.end():]

    for m in _SIZE_PATTERN.finditer(remaining):
        found.append(f"{m.group(1)}x{m.group(2)}")

    if len(found) >= 2:
        return "&".join(found)
    return keyword.strip()


def _track_attribute_terms(segment_key: str, attribute_filters: dict[str, set[str]]) -> None:
    """Record post-filter terms for each attribute type in the query."""
    key = segment_key.strip().lower()
    if not key:
        return

    if key in _COLOR_ALIASES:
        attribute_filters["color"].add(key)
        for part in _COLOR_ALIASES[key].split("||"):
            attribute_filters["color"].add(part.strip().lower())
        return

    if key in _SHAPE_ALIASES:
        attribute_filters["shape"].add(_SHAPE_ALIASES[key].lower())
        attribute_filters["shape"].add(key)
        return

    if key in _PATTERN_ALIASES:
        attribute_filters["pattern"].add(_PATTERN_ALIASES[key].lower())
        attribute_filters["pattern"].add(key)
        return

    if _SIZE_PATTERN.search(key):
        m = _SIZE_PATTERN.search(key)
        if m:
            attribute_filters["size"].add(f"{m.group(1)}x{m.group(2)}".lower())
        return

    for kw in sorted(_CONSTRUCTION_KEYWORDS, key=len, reverse=True):
        if kw in key:
            attribute_filters["construction"].add(kw)
            return

    for kw in sorted(_MATERIAL_KEYWORDS, key=len, reverse=True):
        if kw in key:
            attribute_filters["material"].add(kw)
            return

    for room in sorted(_ROOM_KEYWORDS, key=len, reverse=True):
        if room in key:
            attribute_filters["room"].add(room)
            return

    weight_match = _WEIGHT_PATTERN.search(key)
    if weight_match:
        attribute_filters["weight_max"].add(float(weight_match.group(1)))


def _normalise_keyword(keyword: str) -> tuple[dict | None, str, set[str], dict[str, set]]:
    """
    1. Extract price filter (won't be understood by JR API).
    2. Normalise size tokens (8 x 10 → 8x10, 8 by 10 → 8x10).
    3. Drop generic noise words from each segment.
    Returns (price_filter, clean_keyword_for_jr_api, color_check_terms).
    color_check_terms: lowercase OR alternatives from color alias expansions, used for
    post-result relevance filtering.
    """
    keyword = _preprocess_natural_language((keyword or "").strip())
    overall_price_filter = None
    pending_segments: list[str] = []
    clean_segments = []
    color_check_terms: set[str] = set()
    attribute_filters: dict[str, set] = {
        "color": set(), "shape": set(), "size": set(),
        "material": set(), "construction": set(), "pattern": set(),
        "room": set(), "weight_max": set(),
    }

    for segment in keyword.split("&"):
        segment = segment.strip().lower()
        if not segment:
            continue

        segment = _SIZE_PATTERN.sub(lambda m: f"{m.group(1)}x{m.group(2)}", segment)

        price_filter, residual = _extract_price_filter_from_text(segment)
        if price_filter:
            overall_price_filter = price_filter
            segment = residual.strip()

        words = [w for w in segment.split() if w.lower() not in _NOISE_WORDS]
        segment = " ".join(words).strip()

        if segment:
            pending_segments.append(segment)

    multi_attribute = len(pending_segments) > 1

    for segment in pending_segments:
        expanded = _expand_term(segment, multi_attribute=multi_attribute)
        clean_segments.append(expanded)

        key = segment.strip().lower()
        _track_attribute_terms(key, attribute_filters)

        if key in _COLOR_ALIASES:
            for part in expanded.split("||"):
                color_check_terms.add(part.strip().lower())
        elif "||" in segment:
            for part in segment.split("||"):
                part_key = part.strip().lower()
                if part_key in _COLOR_ALIASES:
                    for term in _COLOR_ALIASES[part_key].split("||"):
                        color_check_terms.add(term.strip().lower())

    if not color_check_terms and attribute_filters["color"]:
        color_check_terms = set(attribute_filters["color"])

    clean_keyword = "&".join(clean_segments)
    return overall_price_filter, clean_keyword, color_check_terms, attribute_filters


def normalise_search_keyword(keyword: str) -> str:
    """Return the cleaned/expanded keyword that will be sent to the JR API."""
    _, clean, _, _ = _normalise_keyword(keyword)
    return clean


def resolve_search_keyword(args: dict | None, user_message: str = "") -> str:
    """Resolve keyword from tool args, tolerating alternate arg names from the model."""
    for key in ("keyword", "query", "search", "search_keyword", "keywords"):
        value = (args or {}).get(key)
        if value and str(value).strip():
            return str(value).strip()
    return (user_message or "").strip()

# ---------------------------------------------------------------------------
# MongoDB product search — mirrors JR API field routing + post-filters
# ---------------------------------------------------------------------------

def _collect_known_catalog_values() -> tuple[set[str], set[str]]:
    colors: set[str] = set()
    patterns: set[str] = set()
    for key, expansion in _COLOR_ALIASES.items():
        colors.add(key.lower())
        for part in expansion.split("||"):
            colors.add(part.strip().lower())
    for key, value in _PATTERN_ALIASES.items():
        patterns.add(key.lower())
        patterns.add(value.lower())
    return colors, patterns


_KNOWN_COLOR_VALUES, _KNOWN_PATTERN_VALUES = _collect_known_catalog_values()
_KNOWN_SHAPE_VALUES = {k.lower() for k in _SHAPE_ALIASES} | {v.lower() for v in _SHAPE_ALIASES.values()}

# All searchable product fields from JR Product Master / Search API (PDF spec)
_API_SEARCH_FIELDS = (
    "ProductType", "Name", "Collection", "Design", "SKU", "BarCode", "ProductURL",
    "GrColor", "BrColor", "ColorFamily", "DisplayFilter", "ColorMood", "BasicColor",
    "Style", "StylePattern", "Pattern", "DecoreStyle", "Designer",
    "SizeInFT", "SizeGroupInFT", "SizeInCM", "SizeGroupInCM",
    "Material", "MaterialDetails", "MaterialFamilies",
    "Construction", "Quality", "Texture", "Shape",
    "Room", "MultiFilter", "FullDescription", "ShortDescription",
    "PileThickness", "ProductTag",
)

_MONGO_FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "color": (
        "raw.GrColor", "raw.BrColor", "raw.ColorFamily",
        "raw.DisplayFilter", "raw.ColorMood", "raw.BasicColor",
    ),
    "pattern": ("raw.Pattern", "raw.Style", "raw.StylePattern", "raw.DecoreStyle"),
    "size": ("raw.SizeInFT", "raw.SizeInCM"),
    "material": ("raw.Material", "raw.MaterialDetails", "raw.MaterialFamilies"),
    "construction": ("raw.Construction",),
    "shape": ("raw.Shape",),
    "room": ("raw.Room", "raw.MultiFilter"),
    "general": tuple(f"raw.{f}" for f in _API_SEARCH_FIELDS),
}


def _size_regex(size_text: str) -> str:
    """Build a flexible size regex (8x10, 8'x10) without matching 18x10 for 8x10."""
    m = _SIZE_PATTERN.search(size_text.strip())
    if not m:
        return re.escape(size_text.strip())
    a, b = m.group(1), m.group(2)
    return rf"(?<!\d){a}\s*['']?\s*[xX*]\s*{b}(?!\d)"


def _classify_segment(segment: str) -> str:
    """Route each keyword segment to the same field group the JR API uses."""
    parts = [p.strip() for p in segment.split("||") if p.strip()]
    if not parts:
        return "general"

    lowered = [p.lower() for p in parts]
    seg_lower = segment.lower()

    if len(parts) == 1 and _SIZE_PATTERN.search(parts[0]):
        return "size"

    if all(p in _KNOWN_SHAPE_VALUES for p in lowered):
        return "shape"
    if len(parts) == 1 and parts[0].lower() in _KNOWN_SHAPE_VALUES:
        return "shape"

    color_hits = sum(1 for p in lowered if p in _KNOWN_COLOR_VALUES)
    if color_hits == len(parts):
        return "color"

    if all(p in _KNOWN_PATTERN_VALUES for p in lowered):
        return "pattern"

    for kw in sorted(_CONSTRUCTION_KEYWORDS, key=len, reverse=True):
        if kw in seg_lower:
            return "construction"

    for kw in sorted(_MATERIAL_KEYWORDS, key=len, reverse=True):
        if kw in seg_lower:
            return "material"

    for room in sorted(_ROOM_KEYWORDS, key=len, reverse=True):
        if room in seg_lower:
            return "room"

    if _WEIGHT_PATTERN.search(seg_lower):
        return "weight"

    if color_hits > 0:
        return "color"

    return "general"


def _segment_to_mongo_clause(segment: str) -> dict:
    """Build a MongoDB clause for one &-segment, scoped to the correct fields."""
    parts = [p.strip() for p in segment.split("||") if p.strip()]
    if not parts:
        return {}

    segment_type = _classify_segment(segment)
    fields = _MONGO_FIELDS_BY_TYPE[segment_type]
    or_clauses = []

    for part in parts:
        pattern = (
            _size_regex(part) if segment_type == "size"
            else re.escape(part)
        )
        for field in fields:
            or_clauses.append({field: {"$regex": pattern, "$options": "i"}})

    return {"$or": or_clauses} if or_clauses else {}


def _part_matches_product(product: dict, part: str, segment_type: str) -> bool:
    """In-memory segment match — mirrors how JR API results are interpreted."""
    fields = [f.replace("raw.", "") for f in _MONGO_FIELDS_BY_TYPE[segment_type]]
    part_lower = part.strip().lower()
    if not part_lower:
        return False

    if segment_type == "color":
        if _color_field_matches(part_lower, str(product.get("GrColor") or "")):
            return True
        for field in ("BrColor", "ColorFamily", "DisplayFilter", "ColorMood"):
            if _color_field_matches(part_lower, str(product.get(field) or "")):
                return True
        return False

    if segment_type == "shape":
        return _shape_field_matches(part_lower, str(product.get("Shape") or ""))

    if segment_type == "weight":
        weight_match = _WEIGHT_PATTERN.search(part_lower)
        if not weight_match:
            return False
        max_kg = float(weight_match.group(1))
        try:
            weight = float(product.get("Weight") or 0)
        except (TypeError, ValueError):
            return False
        return 0 < weight <= max_kg

    if segment_type == "room":
        for field in ("Room", "MultiFilter"):
            if part_lower in str(product.get(field) or "").lower():
                return True
        return False

    if segment_type == "general":
        for field in _API_SEARCH_FIELDS:
            if part_lower in str(product.get(field) or "").lower():
                return True
        return False

    for field in fields:
        val = str(product.get(field) or "")
        if segment_type == "size":
            if re.search(_size_regex(part), val, re.IGNORECASE):
                return True
        elif part_lower in val.lower():
            return True
    return False


def _product_matches_segment(product: dict, segment: str) -> bool:
    """One &-segment must match (OR across || alternatives)."""
    parts = [p.strip() for p in segment.split("||") if p.strip()]
    if not parts:
        return True
    segment_type = _classify_segment(segment)
    return any(_part_matches_product(product, p, segment_type) for p in parts)


def _filter_products_by_clean_keyword(products: list[dict], clean_keyword: str) -> list[dict]:
    """Apply the same &-segment AND logic the JR API search uses."""
    segments = [s.strip() for s in clean_keyword.split("&") if s.strip()]
    if not segments:
        return products
    return [p for p in products if all(_product_matches_segment(p, seg) for seg in segments)]


def _mongo_get_instock_products() -> list[dict]:
    """Return all in-stock products synced from JR Product Master API."""
    cursor = products_collection.find({"flags.inStock": True}, {"_id": 0, "raw": 1})
    return [doc["raw"] for doc in cursor if doc.get("raw")]


def _mongo_search_products(clean_keyword: str, candidate_limit: int = 5000) -> list[dict]:
    """Primary search — mirrors JR product-master-search (& = AND, || = OR).

    Uses MongoDB for fast candidate retrieval, then applies identical in-memory
    segment rules as the website search API on synced Product Master data.
    """
    if not clean_keyword:
        return []

    segments = [s.strip() for s in clean_keyword.split("&") if s.strip()]
    segment_types = [_classify_segment(s) for s in segments]
    logger.info(
        f"[MONGO] search clean_keyword={clean_keyword!r} "
        f"segments={list(zip(segments, segment_types))}"
    )

    and_clauses = [clause for seg in segments if (clause := _segment_to_mongo_clause(seg))]
    query: dict = {"flags.inStock": True}
    if and_clauses:
        query["$and"] = and_clauses

    cursor = (
        products_collection.find(query, {"_id": 0, "raw": 1})
        .sort("raw.ModifyDate", -1)
        .limit(candidate_limit)
    )
    candidates = [doc["raw"] for doc in cursor if doc.get("raw")]
    results = _filter_products_by_clean_keyword(candidates, clean_keyword)

    if not results and and_clauses:
        logger.info("[MONGO] strict query returned 0 — retrying with in-stock scan")
        all_instock = _mongo_get_instock_products()
        results = _filter_products_by_clean_keyword(all_instock, clean_keyword)
        logger.info(
            f"[MONGO] full scan {len(all_instock)} in-stock → {len(results)} matched"
        )
    else:
        logger.info(
            f"[MONGO] {len(candidates)} candidates → {len(results)} matched after validation"
        )
    return results


def _cache_api_search(clean_keyword: str, products: list[dict]) -> None:
    """Persist JR API search results so MongoDB fallback returns the same products."""
    if not clean_keyword or not products:
        return
    try:
        search_cache_collection.update_one(
            {"keyword": clean_keyword},
            {
                "$set": {
                    "keyword": clean_keyword,
                    "products": products,
                    "count": len(products),
                    "updated_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )
        logger.info(f"[CACHE] saved {len(products)} product(s) for keyword={clean_keyword!r}")
    except Exception as e:
        logger.warning(f"[CACHE] failed to save search cache: {e}")


def _load_cached_search(clean_keyword: str) -> list[dict]:
    """Return cached JR API results for this keyword, if available."""
    if not clean_keyword:
        return []
    try:
        doc = search_cache_collection.find_one(
            {"keyword": clean_keyword},
            {"_id": 0, "products": 1},
        )
        products = (doc or {}).get("products") or []
        if products:
            logger.info(f"[CACHE] hit keyword={clean_keyword!r} → {len(products)} product(s)")
        return products
    except Exception as e:
        logger.warning(f"[CACHE] failed to load search cache: {e}")
        return []


def _mongo_get_all_instock_products() -> list[dict]:
    return _mongo_get_instock_products()


def _color_field_matches(term: str, field_value: str) -> bool:
    """Match catalog color values including compound families like 'Pink and Purple'."""
    value = (field_value or "").lower().strip()
    if not value or not term:
        return False
    if term in value:
        return True
    return any(part.strip() == term for part in value.replace("&", " and ").split(" and "))


def _shape_field_matches(term: str, shape_value: str) -> bool:
    shape = (shape_value or "").lower().strip()
    if not shape or not term:
        return False
    catalog = _SHAPE_ALIASES.get(term.lower(), term)
    return shape == catalog.lower() or term.lower() == shape


def _apply_attribute_post_filters(
    products: list[dict],
    attribute_filters: dict[str, set[str]],
) -> list[dict]:
    """Enforce all non-price attributes the user asked for (shape, size, material, etc.)."""
    result = products

    shape_terms = attribute_filters.get("shape") or set()
    if shape_terms:
        filtered = [
            p for p in result
            if any(_shape_field_matches(t, str(p.get("Shape") or "")) for t in shape_terms)
        ]
        if filtered:
            logger.info(f"[SEARCH] shape filter: {len(result)} → {len(filtered)} (terms={shape_terms})")
            result = filtered
        else:
            logger.warning(f"[SEARCH] shape filter removed all products (terms={shape_terms})")
            return []

    weight_terms = attribute_filters.get("weight_max") or set()
    if weight_terms:
        max_kg = max(weight_terms)
        filtered = [
            p for p in result
            if 0 < float(p.get("Weight") or 0) <= max_kg
        ]
        if filtered:
            logger.info(f"[SEARCH] weight filter: {len(result)} → {len(filtered)} (max={max_kg}kg)")
            result = filtered
        else:
            logger.warning(f"[SEARCH] weight filter removed all products (max={max_kg}kg)")
            return []

    room_terms = attribute_filters.get("room") or set()
    if room_terms:
        filtered = [
            p for p in result
            if any(
                term in str(p.get(field) or "").lower()
                for term in room_terms
                for field in ("Room", "MultiFilter")
            )
        ]
        if filtered:
            logger.info(f"[SEARCH] room filter: {len(result)} → {len(filtered)} (terms={room_terms})")
            result = filtered
        else:
            logger.warning(f"[SEARCH] room filter removed all products (terms={room_terms})")
            return []

    for attr, fields in (
        ("size", ("SizeInFT", "SizeInCM")),
        ("material", ("Material", "MaterialDetails", "MaterialFamilies")),
        ("construction", ("Construction",)),
        ("pattern", ("Pattern", "Style", "StylePattern", "DecoreStyle")),
    ):
        terms = attribute_filters.get(attr) or set()
        if not terms:
            continue
        filtered = []
        for p in result:
            matched = False
            for term in terms:
                if attr == "size":
                    if any(
                        re.search(_size_regex(term), str(p.get(field) or ""), re.IGNORECASE)
                        for field in fields
                    ):
                        matched = True
                        break
                elif any(term in str(p.get(field) or "").lower() for field in fields):
                    matched = True
                    break
            if matched:
                filtered.append(p)
        if filtered:
            logger.info(f"[SEARCH] {attr} filter: {len(result)} → {len(filtered)} (terms={terms})")
            result = filtered
        else:
            logger.warning(f"[SEARCH] {attr} filter removed all products (terms={terms})")
            return []

    return result


def _apply_search_pipeline(
    raw_results: list[dict],
    *,
    color_check_terms: set[str],
    attribute_filters: dict[str, set],
    price_filter: dict | None,
    exclude_skus: set | None,
) -> list[dict]:
    """Shared post-processing pipeline — same filters for MongoDB and API results."""
    unique_results = _dedupe_by_sku(raw_results)
    logger.info(f"[SEARCH] after dedup: {len(unique_results)} unique products")

    if color_check_terms:
        _ALL_COLOR_FIELDS = ("GrColor", "BrColor", "ColorFamily", "DisplayFilter", "ColorMood", "BasicColor")
        gr_filtered = [
            p for p in unique_results
            if any(_color_field_matches(term, str(p.get("GrColor") or "")) for term in color_check_terms)
        ]
        if gr_filtered:
            logger.info(
                f"[SEARCH] GrColor filter: {len(unique_results)} → {len(gr_filtered)} "
                f"(terms={color_check_terms})"
            )
            unique_results = gr_filtered
        else:
            all_color_filtered = [
                p for p in unique_results
                if any(
                    _color_field_matches(term, str(p.get(field) or ""))
                    for term in color_check_terms
                    for field in _ALL_COLOR_FIELDS
                )
            ]
            if all_color_filtered:
                logger.info(
                    f"[SEARCH] color fallback filter: {len(unique_results)} → {len(all_color_filtered)} "
                    f"(terms={color_check_terms})"
                )
                unique_results = all_color_filtered

    unique_results = _apply_attribute_post_filters(unique_results, attribute_filters)
    if not unique_results:
        return []

    if price_filter:
        before = len(unique_results)
        unique_results = _apply_price_filter(unique_results, price_filter)
        logger.info(
            f"[SEARCH] price post-filter={price_filter} → "
            f"{before} before, {len(unique_results)} after"
        )

    if exclude_skus:
        upper_exclude = {s.upper() for s in exclude_skus if s}
        unique_results = [
            p for p in unique_results
            if str(p.get("SKU", "")).upper() not in upper_exclude
        ]

    return unique_results

# ---------------------------------------------------------------------------
# JR API helpers
# ---------------------------------------------------------------------------

def _first_valid_image(p: dict) -> str:
    for key in ("HeadShot", "Corner", "CloseUp", "FoldShot", "Floorshot"):
        v = (p.get(key) or "").strip()
        if v and not v.endswith("/"):
            return v
    return ""


def _dedupe_by_sku(products: list[dict]) -> list[dict]:
    unique, seen = [], set()
    for p in products:
        key = str(p.get("SKU") or p.get("BarCode") or "").strip().upper()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(p)
    return unique

# ---------------------------------------------------------------------------
# Public search function
# ---------------------------------------------------------------------------

async def jaipur_rugs_product_search(
    keyword: str,
    client_ip: str = "",
    country_code: str = "",
    requested_currency: str = "",
    exclude_skus: set | None = None,
):
    """Search products — MongoDB primary (synced Product Master), API fallback."""
    try:
        keyword = (keyword or "").strip()
        requested_currency = (
            _normalize_currency_code(requested_currency)
            if requested_currency
            else _extract_requested_currency_from_text(keyword)
        )
        logger.info(
            f"[SEARCH] start — keyword={keyword!r} "
            f"requested_currency={requested_currency!r} "
            f"country_code={country_code!r} client_ip={client_ip!r}"
        )

        # Extract price + clean keyword so JR API only gets text/size/color/style terms
        price_filter, clean_keyword, color_check_terms, attribute_filters = _normalise_keyword(keyword)
        logger.info(
            f"[SEARCH] normalised — clean_keyword={clean_keyword!r} "
            f"price_filter={price_filter} color_check_terms={color_check_terms} "
            f"attribute_filters={attribute_filters}"
        )

        if not clean_keyword and not price_filter:
            logger.warning("[SEARCH] no searchable terms after normalisation")
            return {"error": "No products found."}

        raw_results: list[dict] = []
        search_source = ""

        if not clean_keyword:
            logger.info("[SEARCH] price-only query — full in-stock catalogue from MongoDB")
            raw_results = _mongo_get_all_instock_products()
            search_source = "mongo-catalogue"
            if not raw_results:
                from qlink_chatbot.utils.jr_api_client import get_all_products as _jr_all
                raw_results = await _jr_all()
                search_source = "api-catalogue-fallback"
        else:
            raw_results = _mongo_search_products(clean_keyword)
            search_source = "mongo-search"

            if not raw_results:
                logger.info("[SEARCH] MongoDB empty — falling back to JR product-master-search API")
                try:
                    raw_results = await _jr_search_products(clean_keyword)
                    search_source = "api-search-fallback"
                except Exception as api_err:
                    logger.warning(f"[SEARCH] API fallback failed: {api_err}")

        logger.info(f"[SEARCH] raw results from {search_source}: {len(raw_results)}")

        if not raw_results:
            logger.warning(f"[SEARCH] no results for clean_keyword={clean_keyword!r}")
            return {"error": "No products found."}

        for _i, _p in enumerate(raw_results[:3]):
            logger.info(
                f"[SEARCH] raw[{_i}] SKU={_p.get('SKU')!r} Shape={_p.get('Shape')!r} "
                f"GrColor={_p.get('GrColor')!r} ColorFamily={_p.get('ColorFamily')!r} "
                f"SizeInFT={_p.get('SizeInFT')!r} Material={_p.get('Material')!r} "
                f"Construction={_p.get('Construction')!r} Pattern={_p.get('Pattern')!r}"
            )

        unique_results = _apply_search_pipeline(
            raw_results,
            color_check_terms=color_check_terms,
            attribute_filters=attribute_filters,
            price_filter=price_filter,
            exclude_skus=exclude_skus,
        )
        if not unique_results:
            logger.warning("[SEARCH] 0 products after filters")
            return {"error": "No products found."}

        selected = random.sample(unique_results, min(3, len(unique_results)))
        logger.info(f"[SEARCH] selected {len(selected)} product(s) for response")

        # Resolve display currency
        currency = requested_currency or ""
        if not currency and price_filter:
            currency = price_filter.get("currency", "")
        if not currency:
            currency = _resolve_currency_from_country_code(country_code)
        if not currency:
            currency = await _resolve_currency_from_ip(client_ip)
        currency_field = CURRENCY_FIELDS.get(currency, "INR_MRP")
        logger.info(
            f"[CURRENCY] requested={requested_currency!r} "
            f"resolved={currency!r} field={currency_field}"
        )

        formatted = []
        for p in selected:
            sku = str(p.get("SKU") or p.get("BarCode") or "").strip()
            barcode = str(p.get("BarCode") or "").strip()
            price_amount = p.get(currency_field)
            display_price = _build_display_price(currency, price_amount)

            formatted.append({
                "url": f"https://www.jaipurrugs.com/in/rugs/{p.get('ProductURL')}?barcode={barcode}",
                "price": {"currency": currency, "amount": price_amount},
                "display_currency": currency,
                "display_price": display_price,
                "price_source_field": currency_field,
                "name": (p.get("Name") or p.get("Collection") or "").strip(),
                "SKU": sku,
                "collection": p.get("Collection", ""),
                "size": p.get("SizeInFT", ""),
                "shape": p.get("Shape", ""),
                "color": p.get("GrColor", ""),
                "border_color": p.get("BrColor", ""),
                "color_family": p.get("ColorFamily", ""),
                "display_filter": p.get("DisplayFilter", ""),
                "color_mood": p.get("ColorMood", ""),
                "pattern": p.get("Pattern", ""),
                "matched_color_percentage": {
                    "total": 0,
                    "by_color": {},
                    "highest": {"color": "", "percentage": 0},
                },
                "style": p.get("Style", ""),
                "construction": p.get("Construction", ""),
                "material": p.get("Material", ""),
                "fabric": p.get("MaterialDetails", ""),
                "quality": p.get("Quality", ""),
                "room": [r.strip() for r in (p.get("Room") or "").split(",") if r.strip()],
                "weight": p.get("Weight", 0.0),
                "image": _first_valid_image(p),
                "mrp": {
                    "INR": p.get("INR_MRP"),
                    "USD": p.get("USD_MRP"),
                    "EUR": p.get("EUR_MRP"),
                    "GBP": p.get("GBP_MRP"),
                    "AUD": p.get("AUD_MRP"),
                    "CHF": p.get("CHF_MRP"),
                    "SGD": p.get("SGD_MRP"),
                    "AED": p.get("AED_MRP"),
                },
            })

        final_log = [
            {
                "SKU": i["SKU"],
                "name": i["name"],
                "display_price": i["display_price"],
                "GrColor": i["color"],
                "BrColor": i["border_color"],
                "ColorFamily": i["color_family"],
                "DisplayFilter": i["display_filter"],
                "ColorMood": i["color_mood"],
                "Pattern": i["pattern"],
                "Style": i["style"],
                "size": i["size"],
            }
            for i in formatted
        ]
        logger.info(f"[SEARCH] final payload: {final_log}")
        logger.info(f"[SEARCH] returning {len(formatted)} product(s) for keyword={keyword!r}")
        return formatted

    except Exception as e:
        logger.error(f"[SEARCH] unexpected error: {e}")
        return {"error": f"Unexpected error: {str(e)}"}
