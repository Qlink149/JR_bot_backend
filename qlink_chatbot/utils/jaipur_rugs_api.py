import random
import re

import httpx

from qlink_chatbot.database.mongo_utils import db
from qlink_chatbot.utils.jr_api_client import search_products as _jr_search_products
from qlink_chatbot.utils.logger_config import logger

# Kept for dashboard_routes imports
products_collection = db["products"]
product_color_collection = db["product_color"]

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
_NOISE_WORDS = {
    "show", "me", "find", "search", "looking", "look", "need", "want",
    "please", "rug", "rugs", "carpet", "carpets", "in", "the", "a", "an",
    "and", "or", "with", "of", "for",
}


# Maps user color/pattern words → exact JR API catalog values (GrColor, ColorFamily, DisplayFilter)
# Using || so the API text-searches across all those field values simultaneously.
_COLOR_ALIASES: dict[str, str] = {
    # Reds / Oranges / Rusts
    "red":          "Red||Crimson||Rust||Red and Orange",
    "crimson":      "Crimson||Red||Red and Orange",
    "rust":         "Rust||Copper||Copper Tan||Red and Orange",
    "terracotta":   "Rust||Copper||Red and Orange",
    "orange":       "Red and Orange||Copper||Rust",
    "copper":       "Copper||Copper Tan||Rust||Red and Orange",
    "maroon":       "Crimson||Red and Orange",
    "burgundy":     "Crimson||Red and Orange",
    # Blues
    "blue":         "Blue and Green||Navy Blue",
    "navy":         "Navy Blue||Blue and Green",
    "navy blue":    "Navy Blue||Blue and Green",
    "teal":         "Blue and Green",
    "indigo":       "Navy Blue||Blue and Green",
    # Greens
    "green":        "Blue and Green",
    "olive":        "Blue and Green",
    "sage":         "Blue and Green",
    "emerald":      "Blue and Green",
    # Greys / Blacks
    "grey":         "Classic Gray||Charcoal||Grey and Black",
    "gray":         "Classic Gray||Charcoal||Grey and Black",
    "charcoal":     "Charcoal||Classic Gray||Grey and Black",
    "silver":       "Classic Gray||Grey and Black",
    "black":        "Grey and Black||Charcoal",
    "dark":         "Charcoal||Grey and Black||Navy Blue",
    # Whites / Ivories / Creams
    "white":        "White and Ivory||Ivory||Antique White",
    "ivory":        "Ivory||Antique White||White and Ivory",
    "cream":        "Ivory||Antique White||White and Ivory",
    "off-white":    "Ivory||Antique White||White and Ivory",
    "off white":    "Ivory||Antique White||White and Ivory",
    # Beiges / Browns / Sands
    "beige":        "Beige||Sand||Beige and Brown",
    "sand":         "Sand||Beige||Beige and Brown",
    "tan":          "Sand||Beige||Beige and Brown||Copper Tan",
    "taupe":        "Beige||Sand||Beige and Brown",
    "brown":        "Beige and Brown",
    "chocolate":    "Beige and Brown",
    # Golds / Yellows
    "gold":         "Gold||Yellow and Gold",
    "golden":       "Gold||Yellow and Gold",
    "mustard":      "Gold||Yellow and Gold",
    "yellow":       "Yellow and Gold",
    # Pinks / Purples
    "pink":         "Pink and Purple",
    "blush":        "Pink and Purple",
    "rose":         "Pink and Purple",
    "purple":       "Pink and Purple",
    "lavender":     "Pink and Purple",
    "violet":       "Pink and Purple",
    # Multi
    "multicolor":   "Multi",
    "multi":        "Multi",
    "multi color":  "Multi",
    "multicolour":  "Multi",
    "colorful":     "Multi",
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


def _expand_term(segment: str) -> str:
    """Expand a single keyword segment using color or pattern catalog aliases."""
    key = segment.strip().lower()
    return _COLOR_ALIASES.get(key) or _PATTERN_ALIASES.get(key) or segment


def _normalise_keyword(keyword: str) -> tuple[dict | None, str]:
    """
    1. Extract price filter (won't be understood by JR API).
    2. Normalise size tokens (8 x 10 → 8x10, 8 by 10 → 8x10).
    3. Drop generic noise words from each segment.
    Returns (price_filter, clean_keyword_for_jr_api).
    """
    overall_price_filter = None
    clean_segments = []

    for segment in keyword.split("&"):
        segment = segment.strip().lower()
        if not segment:
            continue

        # Normalise size formats before price extraction so "8 x 10" isn't consumed
        segment = _SIZE_PATTERN.sub(lambda m: f"{m.group(1)}x{m.group(2)}", segment)

        price_filter, residual = _extract_price_filter_from_text(segment)
        if price_filter:
            overall_price_filter = price_filter
            segment = residual.strip()

        # Drop noise from residual
        words = [w for w in segment.split() if w.lower() not in _NOISE_WORDS]
        segment = " ".join(words).strip()

        if segment:
            clean_segments.append(_expand_term(segment))

    clean_keyword = "&".join(clean_segments)
    return overall_price_filter, clean_keyword

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
    """Search products via JR external API (product-master-search)."""
    try:
        requested_currency = (
            _normalize_currency_code(requested_currency)
            if requested_currency
            else _extract_requested_currency_from_text(keyword)
        )
        logger.info(
            f"[JR-API] search start — keyword={keyword!r} "
            f"requested_currency={requested_currency!r} "
            f"country_code={country_code!r} client_ip={client_ip!r}"
        )

        # Extract price + clean keyword so JR API only gets text/size/color/style terms
        price_filter, clean_keyword = _normalise_keyword(keyword)
        logger.info(
            f"[JR-API] normalised — clean_keyword={clean_keyword!r} "
            f"price_filter={price_filter}"
        )

        # Price-only query: no keyword terms remain after extraction.
        # Use product-master (full catalogue) so the price post-filter has data to work on.
        if not clean_keyword:
            logger.info("[JR-API] price-only query — fetching full product catalogue")
            from qlink_chatbot.utils.jr_api_client import get_all_products as _jr_all
            raw_results = await _jr_all()
        else:
            raw_results = await _jr_search_products(clean_keyword)

        logger.info(f"[JR-API] raw results from JR API: {len(raw_results)}")

        if not raw_results:
            logger.warning(f"[JR-API] no results for clean_keyword={clean_keyword!r}")
            return {"error": "No products found."}

        # Log color/pattern fields from first 3 raw products so we can verify catalog values
        for _i, _p in enumerate(raw_results[:3]):
            logger.info(
                f"[JR-API] raw[{_i}] SKU={_p.get('SKU')!r} "
                f"GrColor={_p.get('GrColor')!r} BrColor={_p.get('BrColor')!r} "
                f"ColorFamily={_p.get('ColorFamily')!r} DisplayFilter={_p.get('DisplayFilter')!r} "
                f"ColorMood={_p.get('ColorMood')!r} Pattern={_p.get('Pattern')!r} "
                f"Style={_p.get('Style')!r} Construction={_p.get('Construction')!r} "
                f"Material={_p.get('Material')!r} SizeInFT={_p.get('SizeInFT')!r}"
            )

        unique_results = _dedupe_by_sku(raw_results)
        logger.info(f"[JR-API] after dedup: {len(unique_results)} unique products")

        # Apply price post-filter
        if price_filter:
            before = len(unique_results)
            unique_results = _apply_price_filter(unique_results, price_filter)
            logger.info(
                f"[JR-API] price post-filter={price_filter} → "
                f"{before} before, {len(unique_results)} after"
            )

        if not unique_results:
            logger.warning("[JR-API] 0 products after price filter")
            return {"error": "No products found."}

        if exclude_skus:
            upper_exclude = {s.upper() for s in exclude_skus if s}
            before = len(unique_results)
            unique_results = [
                p for p in unique_results
                if str(p.get("SKU", "")).upper() not in upper_exclude
            ]
            logger.info(
                f"[JR-API] SKU exclusion: removed {before - len(unique_results)}, "
                f"{len(unique_results)} remaining"
            )

        selected = random.sample(unique_results, min(3, len(unique_results)))
        logger.info(f"[JR-API] selected {len(selected)} product(s) for response")

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
        logger.info(f"[JR-API] final payload: {final_log}")
        logger.info(f"[JR-API] returning {len(formatted)} product(s) for keyword={keyword!r}")
        return formatted

    except Exception as e:
        logger.error(f"[JR-API] unexpected error: {e}")
        return {"error": f"Unexpected error: {str(e)}"}
