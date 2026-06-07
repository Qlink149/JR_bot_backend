import random
import re

import httpx

from qlink_chatbot.database.mongo_utils import db
from qlink_chatbot.utils.logger_config import logger

products_collection = db["products"]
product_color_collection = db["product_color"]

COMMON_COLORS = {
    "red", "blue", "green", "yellow", "orange", "purple", "pink", "white",
    "black", "grey", "gray", "brown", "beige", "ivory", "cream", "navy",
    "teal", "turquoise", "gold", "silver", "rust", "terracotta", "coral",
    "indigo", "violet", "maroon", "burgundy", "multi", "multicolor",
    "charcoal", "tan", "olive", "sage", "mustard", "peach", "lavender",
    "copper", "rose", "blush", "aqua", "lime",
}

KNOWN_MATERIALS = {
    "wool", "silk", "viscose", "cotton", "jute", "hemp", "polyester",
    "acrylic", "bamboo", "zari", "linen", "nylon",
}

KNOWN_CONSTRUCTIONS = {
    "hand knotted", "hand tufted", "hand loom", "flat weave", "flat weaves",
    "shag", "handloom", "handknotted", "handtufted",
}

KNOWN_STYLES = {
    "modern", "traditional", "contemporary", "bohemian", "abstract",
    "geometric", "floral", "tribal", "oriental", "transitional", "antique",
    "vintage", "rustic", "minimalist",
}

CURRENCY_FIELDS = {
    "INR": "INR_MRP", "USD": "USD_MRP", "EUR": "EUR_MRP",
    "GBP": "GBP_MRP", "AUD": "AUD_MRP", "CHF": "CHF_MRP",
    "SGD": "SGD_MRP", "AED": "AED_MRP",
}

CURRENCY_ALIASES = {
    "inr": "INR",
    "rs": "INR",
    "rupee": "INR",
    "rupees": "INR",
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "gbp": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
    "aud": "AUD",
    "chf": "CHF",
    "sgd": "SGD",
    "aed": "AED",
}

PRICE_OPERATOR_WORDS = {
    "above": "$gte",
    "over": "$gte",
    "more than": "$gte",
    "greater than": "$gte",
    "higher than": "$gte",
    "minimum": "$gte",
    "min": "$gte",
    "from": "$gte",
    "starting from": "$gte",
    "at least": "$gte",
    "below": "$lte",
    "under": "$lte",
    "less than": "$lte",
    "lower than": "$lte",
    "maximum": "$lte",
    "max": "$lte",
    "up to": "$lte",
    "upto": "$lte",
    "within": "$lte",
    "budget": "$lte",
    "for": "$lte",
}

PRICE_CONTEXT_WORDS = {
    "price", "priced", "cost", "costing", "amount", "budget", "range",
    "mrp", "rs", "rupee", "rupees", "inr", "usd", "eur", "gbp", "aud",
    "chf", "sgd", "aed", "dollar", "dollars", "euro", "euros", "pound",
    "pounds",
}

AMOUNT_MULTIPLIERS = {
    "k": 1_000,
    "thousand": 1_000,
    "l": 100_000,
    "lac": 100_000,
    "lacs": 100_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lc": 100_000,
    "cr": 10_000_000,
    "crore": 10_000_000,
    "crores": 10_000_000,
    "m": 1_000_000,
    "million": 1_000_000,
}

GENERIC_NOISE_WORDS = {
    "show", "me", "find", "search", "looking", "look", "need", "want",
    "please", "rug", "rugs", "carpet", "carpets", "price", "priced",
    "cost", "costing", "range", "amount", "any", "some", "options",
    "option", "products", "product",
    "inr", "usd", "eur", "gbp", "aud", "chf", "sgd", "aed",
    "dollar", "dollars", "euro", "euros", "pound", "pounds",
}

MATERIAL_QUALIFIER_WORDS = {
    "pure", "all", "full", "fully", "only", "made", "from", "material",
    "materials", "fabric", "fabrics",
}

CALLING_CODE_TO_CURRENCY: dict[str, str] = {
    "91":  "INR",  # India
    "971": "AED",  # UAE
    "966": "AED",  # Saudi Arabia
    "965": "AED",  # Kuwait
    "974": "AED",  # Qatar
    "968": "AED",  # Oman
    "973": "AED",  # Bahrain
    "61":  "AUD",  # Australia
    "41":  "CHF",  # Switzerland
    "423": "CHF",  # Liechtenstein
    "44":  "GBP",  # United Kingdom
    "65":  "SGD",  # Singapore
    "1":   "USD",  # USA / Canada
    "49":  "EUR",  # Germany
    "33":  "EUR",  # France
    "39":  "EUR",  # Italy
    "34":  "EUR",  # Spain
    "31":  "EUR",  # Netherlands
    "32":  "EUR",  # Belgium
    "43":  "EUR",  # Austria
    "351": "EUR",  # Portugal
    "30":  "EUR",  # Greece
    "358": "EUR",  # Finland
    "353": "EUR",  # Ireland
    "352": "EUR",  # Luxembourg
    "356": "EUR",  # Malta
    "386": "EUR",  # Slovenia
    "421": "EUR",  # Slovakia
    "372": "EUR",  # Estonia
    "371": "EUR",  # Latvia
    "370": "EUR",  # Lithuania
    "357": "EUR",  # Cyprus
}
DEFAULT_CURRENCY = "INR"


def _extract_colors_from_text(text: str) -> tuple[list[str], str]:
    """Extract known colors from free text and return (colors, residual_text)."""
    extracted: list[str] = []
    residual = text

    # Match longer color words first (e.g. multicolor before multi).
    for color in sorted(COMMON_COLORS, key=len, reverse=True):
        pattern = rf"\b{re.escape(color)}\b"
        if re.search(pattern, residual):
            extracted.append(color)
            residual = re.sub(pattern, " ", residual)

    # Remove common connectors/noise that appear with color phrases.
    residual = re.sub(r"\b(and|or|with|in|of|for|rug|rugs)\b", " ", residual)
    residual = re.sub(r"\s+", " ", residual).strip()

    return extracted, residual


def _extract_materials_from_text(text: str) -> tuple[list[str], str]:
    """Extract known materials from free text and ignore purity percentages."""
    extracted: list[str] = []
    residual = re.sub(r"\b\d+(?:\.\d+)?\s*%", " ", text)

    for material in sorted(KNOWN_MATERIALS, key=len, reverse=True):
        pattern = rf"\b{re.escape(material)}\b"
        if re.search(pattern, residual):
            extracted.append(material)
            residual = re.sub(pattern, " ", residual)

    residual = re.sub(r"\b(and|or|with|in|of|for|rug|rugs)\b", " ", residual)
    words = [
        word for word in residual.split()
        if word not in MATERIAL_QUALIFIER_WORDS
    ]
    residual = re.sub(r"\s+", " ", " ".join(words)).strip()

    return extracted, residual


def _parse_amount_with_suffix(amount_text: str, suffix: str = "") -> float:
    amount = float(amount_text.replace(",", ""))
    multiplier = AMOUNT_MULTIPLIERS.get((suffix or "").lower(), 1)
    return amount * multiplier


def _is_probable_price_amount(amount: float, suffix: str = "", context: str = "") -> bool:
    if suffix:
        return True
    if amount >= 1000:
        return True
    return any(word in context.split() for word in PRICE_CONTEXT_WORDS)


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
    formatted_amount = _format_price_amount(amount)
    if not formatted_amount:
        return ""
    return f"{currency} {formatted_amount}"


def _currency_alias_pattern() -> str:
    return "|".join(
        re.escape(alias)
        for alias in sorted(CURRENCY_ALIASES, key=len, reverse=True)
    )


def _extract_requested_currency_from_text(text: str) -> str:
    lower = (text or "").lower()
    alias_pattern = _currency_alias_pattern()
    patterns = [
        rf"\bin\s*({alias_pattern})\b",
        rf"\bin({alias_pattern})\b",
        rf"\b({alias_pattern})\s*(?:price|prices|pricing|mrp)\b",
        rf"\b(?:price|prices|pricing|mrp)\s*(?:in\s*)?({alias_pattern})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            currency = _normalize_currency_code(match.group(1))
            if currency in CURRENCY_FIELDS:
                return currency
    return ""


def _clean_generic_residual(text: str) -> str:
    text = re.sub(rf"\bin(?:{_currency_alias_pattern()})\b", " ", text)
    text = re.sub(r"\b(?:and|or|with|in|of|for|the|a|an)\b", " ", text)
    words = [word for word in text.split() if word not in GENERIC_NOISE_WORDS]
    return re.sub(r"\s+", " ", " ".join(words)).strip()


def _extract_price_filter_from_text(text: str) -> tuple[dict | None, str]:
    """Extract one price filter from free text, supporting Indian shorthand."""
    currencies = r"inr|usd|eur|gbp|aud|chf|sgd|aed"
    currency_aliases = _currency_alias_pattern()
    operators = "|".join(
        re.escape(word)
        for word in sorted(PRICE_OPERATOR_WORDS, key=len, reverse=True)
    )
    amount = r"([\d,]+(?:\.\d+)?)\s*(k|thousand|lacs?|lakhs?|lakh|lc|l|cr|crores?|m|million)?"

    range_match = re.search(
        rf"\bbetween\s+(?:({currency_aliases})\s*)?{amount}\s+"
        rf"(?:and|to|-)\s+(?:({currency_aliases})\s*)?{amount}",
        text,
    )
    if range_match:
        first_currency, min_amount_text, min_suffix, second_currency, max_amount_text, max_suffix = (
            range_match.groups()
        )
        min_amount = _parse_amount_with_suffix(min_amount_text, min_suffix or "")
        max_amount = _parse_amount_with_suffix(max_amount_text, max_suffix or min_suffix or "")
        currency = first_currency or second_currency or DEFAULT_CURRENCY
        price_filter = {
            "currency": _normalize_currency_code(currency),
            "min_amount": min(min_amount, max_amount),
            "max_amount": max(min_amount, max_amount),
        }
        residual = f"{text[:range_match.start()]} {text[range_match.end():]}"
        return price_filter, _clean_generic_residual(residual)

    patterns = [
        # "under 1000 usd", "below 500 dollars"
        rf"\b({operators})\b\s*{amount}\s*({currency_aliases})\b",
        # "above INR 4 lakh", "under usd 1000", "budget 80000"
        rf"\b({operators})\b\s*(?:({currency_aliases})\s*)?{amount}",
        # "INR 4 lakh above", "usd 1000 under"
        rf"\b(?:({currency_aliases})\s*)?{amount}\s*\b({operators})\b",
        # Existing compact form: "INR 80000", "rs 50000", "50000 rupees".
        rf"\b({currency_aliases})\s+{amount}\b",
        rf"\b{amount}\s*({currency_aliases})\b",
        # Plain amount with context or a large enough value: "show rugs 100000", "4 lakh".
        rf"\b{amount}\b",
    ]

    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if not match:
            continue

        groups = match.groups()
        if index == 0:
            operator_word, amount_text, suffix, currency = groups
        elif index == 1:
            operator_word, currency, amount_text, suffix = groups
        elif index == 2:
            currency, amount_text, suffix, operator_word = groups
        elif index == 3:
            currency, amount_text, suffix = groups
            operator_word = "budget"
        elif index == 4:
            amount_text, suffix, currency = groups
            operator_word = "budget"
        else:
            amount_text, suffix = groups
            currency = DEFAULT_CURRENCY
            operator_word = "budget"

        parsed_amount = _parse_amount_with_suffix(amount_text, suffix or "")
        if not currency and not _is_probable_price_amount(
            parsed_amount,
            suffix or "",
            f"{text[:match.start()]} {text[match.end():]}",
        ):
            continue

        price_filter = {
            "currency": _normalize_currency_code(currency or DEFAULT_CURRENCY),
            "amount": parsed_amount,
            "operator": PRICE_OPERATOR_WORDS.get(operator_word, "$lte"),
        }
        residual = f"{text[:match.start()]} {text[match.end():]}"
        return price_filter, _clean_generic_residual(residual)

    return None, _clean_generic_residual(text)


def _normalize_sku(value) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()


def _extract_product_sku(product: dict) -> str:
    raw = product.get("raw", {})
    candidates = [
        raw.get("SKU"),
        product.get("SKU"),
        raw.get("BarCode"),
        product.get("BarCode"),
    ]
    for candidate in candidates:
        sku = _normalize_sku(candidate)
        if sku:
            return sku
    return ""


def _color_text_has_requested(color_text: str, requested_color: str) -> bool:
    text = str(color_text or "").lower().strip()
    req = str(requested_color or "").lower().strip()
    if not text or not req:
        return False
    return bool(re.search(rf"(^|[^a-z]){re.escape(req)}([^a-z]|$)", text))


def _color_map_has_all_requested(color_map: dict, requested_colors: list[str]) -> bool:
    requested = [c.lower().strip() for c in requested_colors if c and c.strip()]
    if not requested:
        return True
    color_keys = list(color_map.keys())
    return all(any(_color_text_has_requested(k, req) for k in color_keys) for req in requested)


def _sum_requested_color_percentage(color_map: dict, requested_colors: list[str]) -> float:
    total = 0.0
    requested = [c.lower().strip() for c in requested_colors if c and c.strip()]
    for key, value in color_map.items():
        if any(_color_text_has_requested(key, req) for req in requested):
            total += float(value or 0)
    return total


def _dedupe_products_by_sku(products: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for product in products:
        sku = _extract_product_sku(product)
        raw = product.get("raw", {})
        dedupe_key = sku or _normalize_sku(raw.get("ProductURL")) or _normalize_sku(product.get("BarCode"))
        if dedupe_key and dedupe_key in seen:
            continue
        if dedupe_key:
            seen.add(dedupe_key)
        unique.append(product)
    return unique


def _resolve_color_sku_scores(colors: list[str], limit: int = 1000) -> tuple[list[str], dict]:
    """Return ordered SKUs and a score map from product_color by summing percentages."""
    if not colors:
        return [], {}

    requested_pattern = "|".join(
        rf"(^|[^a-z]){re.escape(c.lower().strip())}([^a-z]|$)"
        for c in colors
        if c and c.strip()
    )

    total_color_docs = product_color_collection.count_documents({})
    raw_collection_sample = list(product_color_collection.find({}, {"_id": 0}).limit(5))
    logger.info(f"product_color total docs: {total_color_docs}")
    logger.info(f"product_color raw sample (max 5): {raw_collection_sample}")

    pipeline = [
        {
            "$project": {
                "sku": {"$ifNull": ["$SKU", "$sku"]},
                "color_raw": {
                    "$ifNull": [
                        "$PrimaryColorName",
                        {
                            "$ifNull": [
                                "$Primary Color Name",
                                {"$ifNull": ["$ColorName", {"$ifNull": ["$color", "$Colour"]}]},
                            ]
                        },
                    ]
                },
                "percentage": {
                    "$ifNull": [
                        "$ColorPercentage",
                        {"$ifNull": ["$Percentage", {"$ifNull": ["$percentage", 0]}]},
                    ]
                },
            }
        },
        {
            "$addFields": {
                "sku": {"$toUpper": {"$trim": {"input": {"$toString": {"$ifNull": ["$sku", ""]}}}}},
                "color": {"$toLower": {"$trim": {"input": {"$toString": {"$ifNull": ["$color_raw", ""]}}}}},
            }
        },
        {"$match": {"color": {"$regex": requested_pattern}}},
        {
            "$group": {
                "_id": {
                    "sku": "$sku",
                    "color": "$color",
                },
                "percentage": {"$sum": {"$ifNull": ["$percentage", 0]}},
            }
        },
        {
            "$group": {
                "_id": "$_id.sku",
                "total_percentage": {"$sum": "$percentage"},
                "colors": {
                    "$push": {
                        "k": "$_id.color",
                        "v": "$percentage",
                    }
                },
            }
        },
        {"$sort": {"total_percentage": -1}},
        {"$limit": limit},
    ]

    sample_pipeline = [
        pipeline[0],
        pipeline[1],
        pipeline[2],
        {"$project": {"_id": 0, "sku": 1, "color": 1, "percentage": 1}},
        {"$limit": 5},
    ]
    color_sample = list(product_color_collection.aggregate(sample_pipeline))
    logger.info(f"product_color normalized sample (max 5): {color_sample}")

    aggregated = list(product_color_collection.aggregate(pipeline))
    logger.info(f"product_color aggregation rows: {len(aggregated)} for colors={colors}")
    ordered_skus: list[str] = []
    sku_scores: dict = {}

    for row in aggregated:
        sku = _normalize_sku(row.get("_id"))
        if not sku:
            continue
        color_pct_map = {}
        for entry in row.get("colors", []):
            color_key = str(entry.get("k", "")).lower().strip()
            if not color_key:
                continue
            color_pct_map[color_key] = float(entry.get("v", 0) or 0)

        # For multi-color queries, keep only SKUs that contain all requested colors.
        if len(colors) > 1 and not _color_map_has_all_requested(color_pct_map, colors):
            continue

        matched_total = _sum_requested_color_percentage(color_pct_map, colors)
        if matched_total <= 0:
            continue

        ordered_skus.append(sku)
        sku_scores[sku] = {
            "total_percentage": float(matched_total),
            "colors": color_pct_map,
        }

    return ordered_skus, sku_scores


def _highest_matched_color(color_map: dict, requested_colors: list[str]) -> tuple[str, float]:
    """Return highest matched requested color and its percentage."""
    if not color_map:
        return "", 0.0

    normalized_requested = [c.lower().strip() for c in requested_colors if c]
    best_color = ""
    best_pct = 0.0
    for color_key, value in color_map.items():
        if not any(_color_text_has_requested(color_key, req) for req in normalized_requested):
            continue
        pct = float(value or 0)
        if pct > best_pct:
            best_color = color_key
            best_pct = pct

    # Fallback to the highest available color if no requested color key matched exactly.
    if not best_color and color_map:
        best_color, best_pct = max(color_map.items(), key=lambda x: float(x[1] or 0))
        best_pct = float(best_pct or 0)

    return best_color, best_pct


def _parse_keyword_filters(keyword: str):
    """
    Parse a &-separated keyword string into structured filters.
    Returns: colors, materials, constructions, styles, sizes, price_filter, weight_filter, generics
    """
    parts = [p.strip() for p in keyword.split("&")]
    colors = []
    materials = []
    constructions = []
    styles = []
    sizes = []
    price_filter = None
    weight_filter = None
    generics = []

    for part in parts:
        lower = part.lower().strip()
        if not lower:
            continue

        # Color phrases inside free text: "red rugs", "red and blue rugs".
        # Keep any remaining text as generic so other filters still work.
        found_colors, residual_text = _extract_colors_from_text(lower)
        if found_colors:
            colors.extend(found_colors)
            lower = residual_text
            if not lower:
                continue

        # Price: "INR 30000", "above 4lc", "under INR 2 lakh".
        parsed_price_filter, lower = _extract_price_filter_from_text(lower)
        if parsed_price_filter:
            price_filter = parsed_price_filter
            if not lower:
                continue

        # Material phrases inside free text: "100% cotton rugs", "pure wool rugs".
        found_materials, residual_text = _extract_materials_from_text(lower)
        if found_materials:
            materials.extend(found_materials)
            lower = residual_text
            if not lower:
                continue

        # Weight ceiling: "weight 8", "8kg", "weight 8kg"
        weight_match = re.match(r'^(?:weight\s*)?([\d.]+)\s*kg?$', lower)
        if weight_match:
            weight_filter = float(weight_match.group(1))
            continue

        # Size: "8x10", "5x7", "9x12"
        if re.match(r'^\d+\s*x\s*\d+$', lower):
            sizes.append(lower.replace(" ", ""))
            continue

        # Color
        if lower in COMMON_COLORS:
            colors.append(lower)
            continue

        # Construction (check before style since "hand knotted" is multi-word)
        if any(c in lower for c in KNOWN_CONSTRUCTIONS):
            constructions.append(lower)
            continue

        # Material
        if lower in KNOWN_MATERIALS:
            materials.append(lower)
            continue

        # Style
        if lower in KNOWN_STYLES:
            styles.append(lower)
            continue

        # Everything else → generic text match
        generic = _clean_generic_residual(lower)
        if generic:
            generics.append(generic)

    # Deduplicate while preserving order so fallback logic sees accurate color count.
    colors = list(dict.fromkeys(colors))
    materials = list(dict.fromkeys(materials))

    return colors, materials, constructions, styles, sizes, price_filter, weight_filter, generics


def _build_mongo_query(
    color_field: str | None,
    colors: list,
    size_field: str | None,
    sizes: list,
    material_field: str | None,
    materials: list,
    constructions: list,
    styles: list,
    price_filter: dict | None,
    generics: list,
    sku_filter: list[str] | None = None,
) -> dict:
    """Build a MongoDB filter dict from the given field choices and filter values."""
    query: dict = {"flags.inStock": True}
    and_clauses = []

    if color_field and colors:
        regex = "|".join(re.escape(c) for c in colors)
        query[color_field] = {"$regex": regex, "$options": "i"}

    if size_field and sizes:
        regex = "|".join(re.escape(s) for s in sizes)
        query[size_field] = {"$regex": regex, "$options": "i"}

    if material_field and materials:
        regex = "|".join(re.escape(m) for m in materials)
        query[material_field] = {"$regex": regex, "$options": "i"}

    if constructions:
        regex = "|".join(re.escape(c) for c in constructions)
        query["search.construction"] = {"$regex": regex, "$options": "i"}

    if styles:
        regex = "|".join(re.escape(s) for s in styles)
        query["search.style"] = {"$regex": regex, "$options": "i"}

    if price_filter:
        comparison = {"$gt": 0}
        if "min_amount" in price_filter:
            comparison["$gte"] = price_filter["min_amount"]
        if "max_amount" in price_filter:
            comparison["$lte"] = price_filter["max_amount"]
        if "amount" in price_filter:
            operator = price_filter.get("operator") or "$lte"
            comparison[operator] = price_filter["amount"]
        if price_filter["currency"] == "INR":
            query["search.price"] = comparison
        else:
            field = CURRENCY_FIELDS.get(price_filter["currency"])
            if field:
                query[f"raw.{field}"] = comparison

    if sku_filter:
        and_clauses.append(
            {
                "$or": [
                    {"raw.SKU": {"$in": sku_filter}},
                    {"SKU": {"$in": sku_filter}},
                    {"raw.BarCode": {"$in": sku_filter}},
                    {"BarCode": {"$in": sku_filter}},
                ]
            }
        )

    if generics:
        regex = "|".join(re.escape(g) for g in generics)
        and_clauses.append(
            {
                "$or": [
                    {"raw.Name": {"$regex": regex, "$options": "i"}},
                    {"raw.Collection": {"$regex": regex, "$options": "i"}},
                    {"raw.Design": {"$regex": regex, "$options": "i"}},
                    {"raw.Quality": {"$regex": regex, "$options": "i"}},
                    {"raw.Shape": {"$regex": regex, "$options": "i"}},
                ]
            }
        )

    if and_clauses:
        query["$and"] = and_clauses

    return query


def _run_query(query: dict, limit: int = 200) -> list:
    return list(products_collection.find(query, {"_id": 0}).limit(limit))


def _apply_weight_filter(products: list, weight_filter: float) -> list:
    """Post-filter by weight ceiling (handles string/float stored values)."""
    result = []
    for p in products:
        try:
            w = p.get("search", {}).get("weight")
            if w is None or float(w) <= weight_filter:
                result.append(p)
        except (TypeError, ValueError):
            result.append(p)
    return result


async def _resolve_currency_from_ip(ip: str) -> str:
    """Look up the currency for a client IP using ip-api.com (free, no key needed)."""
    try:
        async with httpx.AsyncClient(timeout=3) as geo_client:
            resp = await geo_client.get(f"http://ip-api.com/json/{ip}?fields=currency,status")
            data = resp.json()
            if data.get("status") == "success":
                currency = data.get("currency", "").upper()
                if currency in CURRENCY_FIELDS:
                    logger.info(f"Resolved currency {currency} for IP {ip}")
                    return currency
    except Exception as e:
        logger.warning(f"IP geolocation failed for {ip}: {e}")
    return DEFAULT_CURRENCY


def _resolve_currency_from_country_code(country_code: str) -> str:
    digits = re.sub(r"\D", "", country_code or "")
    for length in range(min(3, len(digits)), 0, -1):
        currency = CALLING_CODE_TO_CURRENCY.get(digits[:length])
        if currency:
            return currency
    return ""


def _first_valid_image(raw: dict) -> str:
    for key in ("HeadShot", "Corner", "CloseUp", "Floorshot"):
        value = (raw.get(key) or "").strip()
        if value and not value.endswith("/"):
            return value
    return ""


async def jaipur_rugs_product_search(
    keyword: str,
    client_ip: str = "",
    country_code: str = "",
    requested_currency: str = "",
    exclude_skus: set | None = None,
):
    """Search products from MongoDB with progressive field fallback."""
    try:
        requested_currency = (
            _normalize_currency_code(requested_currency)
            if requested_currency
            else _extract_requested_currency_from_text(keyword)
        )
        colors, materials, constructions, styles, sizes, price_filter, weight_filter, generics = _parse_keyword_filters(keyword)
        logger.info(f"Parsed filters — colors: {colors}, materials: {materials}, sizes: {sizes}, weight: {weight_filter}, price: {price_filter}")

        color_sku_filter: list[str] = []
        color_sku_scores: dict = {}
        query_colors = colors[:]

        # If user asked for colors, first resolve matching SKUs from product_color.
        if colors:
            logger.info(f"Attempting product_color lookup for colors={colors}")
            color_sku_filter, color_sku_scores = _resolve_color_sku_scores(colors)
            if color_sku_filter:
                logger.info(f"Found {len(color_sku_filter)} color-matched SKUs from product_color")
                # Color filtering is now handled by SKU shortlist from product_color.
                query_colors = []
            else:
                logger.info(f"No SKU matched in product_color for colors={colors}")

        # Field fallback sequences per filter type
        # Color: single color → try single field first, then multi; multiple colors → multi only
        if len(query_colors) == 1:
            color_fields = ["search.color.single", "search.color.multi", None]
        elif len(query_colors) > 1:
            color_fields = ["search.color.multi", None]
        else:
            color_fields = [None]

        # Size: exact match → group match
        size_fields = ["search.size.exact", "search.size.group", None] if sizes else [None]

        # Material: primary → family → details
        material_fields = (
            ["search.material.primary", "search.material.family", "search.material.details", None]
            if materials else [None]
        )

        results = []

        # Try all combinations in order, stop at first non-empty result
        for c_field in color_fields:
            if results:
                break
            for s_field in size_fields:
                if results:
                    break
                for m_field in material_fields:
                    query = _build_mongo_query(
                        c_field, query_colors,
                        s_field, sizes,
                        m_field, materials,
                        constructions, styles,
                        price_filter, generics,
                        color_sku_filter,
                    )
                    results = _run_query(query)
                    if results:
                        logger.info(
                            f"Found {len(results)} products "
                            f"[color={c_field}, size={s_field}, material={m_field}]"
                        )
                        break

        # Style fallback: search style keywords in product name/collection/design text
        # (JR API may not populate the Style field consistently)
        if not results and styles:
            style_generics = styles[:]
            query = _build_mongo_query(
                None, query_colors,
                None, sizes,
                None, materials,
                constructions, [],
                price_filter, generics + style_generics,
                color_sku_filter,
            )
            results = _run_query(query)
            if results:
                logger.info(f"Style text-fallback found {len(results)} products for styles={styles}")

        # Fallback: price / weight only (drop keyword filters)
        if not results and (price_filter or weight_filter):
            query = _build_mongo_query(None, [], None, [], None, [], [], [], price_filter, [], color_sku_filter)
            results = _run_query(query)

        # Final fallback: any in-stock product — ONLY when no specific filters were given
        # (never return random products when the user asked for something specific)
        has_specific_filter = any([colors, styles, materials, constructions, sizes, generics])
        if not results and not has_specific_filter:
            results = _run_query({"flags.inStock": True})

        if not results:
            return {"error": "No products found."}

        # Weight is post-filtered in Python (may be stored as string in DB)
        if weight_filter is not None:
            results = _apply_weight_filter(results, weight_filter)

        currency = requested_currency or (price_filter or {}).get("currency") or ""
        if not currency:
            currency = _resolve_currency_from_country_code(country_code)
        if not currency:
            currency = await _resolve_currency_from_ip(client_ip)
        currency_field = CURRENCY_FIELDS.get(currency, "INR_MRP")

        if color_sku_scores:
            # Prefer products with highest matched requested color percentage after all filters.
            results = sorted(
                results,
                key=lambda p: (
                    _highest_matched_color(
                        color_sku_scores.get(_extract_product_sku(p), {}).get("colors", {}),
                        colors,
                    )[1],
                    color_sku_scores.get(_extract_product_sku(p), {}).get("total_percentage", 0),
                ),
                reverse=True,
            )
            unique_results = _dedupe_products_by_sku(results)
        else:
            unique_results = _dedupe_products_by_sku(results)

        if exclude_skus:
            upper_exclude = {s.upper() for s in exclude_skus if s}
            unique_results = [
                p for p in unique_results
                if _extract_product_sku(p).upper() not in upper_exclude
            ]

        if color_sku_scores:
            selected = unique_results[:3]
        else:
            selected = random.sample(unique_results, min(3, len(unique_results)))
        formatted = []
        for p in selected:
            raw = p.get("raw", {})
            search = p.get("search", {})
            color = search.get("color", {})
            material = search.get("material", {})
            size = search.get("size", {})
            sku = _extract_product_sku(p)
            color_score = color_sku_scores.get(sku, {})
            highest_color, highest_percentage = _highest_matched_color(
                color_score.get("colors", {}),
                colors,
            )
            price_amount = raw.get(currency_field)
            display_price = _build_display_price(currency, price_amount)
            formatted.append({
                "url": f"https://www.jaipurrugs.com/in/rugs/{raw.get('ProductURL')}?barcode={p.get('BarCode')}",
                "price": {"currency": currency, "amount": price_amount},
                "display_currency": currency,
                "display_price": display_price,
                "price_source_field": currency_field,
                "name": raw.get("Name", ""),
                "SKU": sku,
                "collection": raw.get("Collection", ""),
                "size": size.get("exact", raw.get("SizeInFT", "")),
                "shape": search.get("shape", raw.get("Shape", "")),
                "color": color.get("single", ""),
                "color_family": color.get("multi", ""),
                "matched_color_percentage": {
                    "total": color_score.get("total_percentage", 0),
                    "by_color": color_score.get("colors", {}),
                    "highest": {
                        "color": highest_color,
                        "percentage": highest_percentage,
                    },
                },
                "style": search.get("style", ""),
                "construction": search.get("construction", ""),
                "material": material.get("primary", ""),
                "fabric": material.get("details", ""),
                "quality": search.get("quality", ""),
                "room": search.get("room", []),
                "weight": search.get("weight", 0.0),
                "image": _first_valid_image(raw),
                "mrp": {
                    "INR": raw.get("INR_MRP"),
                    "USD": raw.get("USD_MRP"),
                    "EUR": raw.get("EUR_MRP"),
                    "GBP": raw.get("GBP_MRP"),
                    "AUD": raw.get("AUD_MRP"),
                    "CHF": raw.get("CHF_MRP"),
                    "SGD": raw.get("SGD_MRP"),
                    "AED": raw.get("AED_MRP"),
                },
            })

        final_log_payload = [
            {
                "SKU": item.get("SKU", ""),
                "matched_color_percentage": item.get("matched_color_percentage", {}),
            }
            for item in formatted
        ]
        logger.info(f"Final products payload (SKU + color %): {final_log_payload}")
        logger.info(f"Returning {len(formatted)} products for keyword: {keyword}")
        return formatted

    except Exception as e:
        logger.error(f"Unexpected error in product search: {e}")
        return {"error": f"Unexpected error: {str(e)}"}
