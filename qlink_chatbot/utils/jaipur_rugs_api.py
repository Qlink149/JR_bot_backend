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
        pattern = rf"\\b{re.escape(color)}\\b"
        if re.search(pattern, residual):
            extracted.append(color)
            residual = re.sub(pattern, " ", residual)

    # Remove common connectors/noise that appear with color phrases.
    residual = re.sub(r"\b(and|or|with|in|of|for|rug|rugs)\b", " ", residual)
    residual = re.sub(r"\s+", " ", residual).strip()

    return extracted, residual


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

        # Price: "INR 30000"
        price_match = re.match(
            r'^(inr|usd|eur|gbp|aud|chf|sgd|aed)\s+([\d,]+(?:\.\d+)?)$', lower
        )
        if price_match:
            price_filter = {
                "currency": price_match.group(1).upper(),
                "amount": float(price_match.group(2).replace(",", "")),
            }
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
        generics.append(lower)

    # Deduplicate while preserving order so fallback logic sees accurate color count.
    colors = list(dict.fromkeys(colors))

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
        if price_filter["currency"] == "INR":
            query["search.price"] = {"$gt": 0, "$lte": price_filter["amount"]}
        else:
            field = CURRENCY_FIELDS.get(price_filter["currency"])
            if field:
                query[f"raw.{field}"] = {"$gt": 0, "$lte": price_filter["amount"]}

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


async def jaipur_rugs_product_search(keyword: str, client_ip: str = "", country_code: str = ""):  # country_code kept for caller compatibility
    """Search products from MongoDB with progressive field fallback."""
    try:
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

        # Fallback: price / weight only (drop keyword filters)
        if not results and (price_filter or weight_filter):
            query = _build_mongo_query(None, [], None, [], None, [], [], [], price_filter, [], color_sku_filter)
            results = _run_query(query)

        # Final fallback: any in-stock product (skip for explicit color queries)
        if not results and not colors:
            results = _run_query({"flags.inStock": True})

        if not results:
            return {"error": "No products found."}

        # Weight is post-filtered in Python (may be stored as string in DB)
        if weight_filter is not None:
            results = _apply_weight_filter(results, weight_filter)

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
            selected = unique_results[:3]
        else:
            unique_results = _dedupe_products_by_sku(results)
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
            formatted.append({
                "url": f"https://www.jaipurrugs.com/in/rugs/{raw.get('ProductURL')}?barcode={p.get('BarCode')}",
                "price": {"currency": currency, "amount": raw.get(currency_field)},
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
                "image": raw.get("HeadShot", ""),
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
