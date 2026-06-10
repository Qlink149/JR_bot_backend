import re

from qlink_chatbot.database.mongo_utils import db
from qlink_chatbot.utils.jr_search_aliases import (
    API_SEARCH_FIELDS,
    CONSTRUCTION_KEYWORDS,
    KNOWN_COLOR_VALUES,
    KNOWN_PATTERN_VALUES,
    KNOWN_SHAPE_VALUES,
    MATERIAL_KEYWORDS,
    MONGO_FIELDS_BY_TYPE,
    MULTICOLOR_KEYS,
    ROOM_KEYWORDS,
    SHAPE_ALIASES,
    SIZE_PATTERN,
    ROUND_SIZE_PATTERN,
    WEIGHT_PATTERN,
    color_search_terms,
    color_match_is_strict,
    COLOR_FAMILY_FIELDS,
    COLOR_GRBR_FIELDS,
)
from qlink_chatbot.utils.jr_search_currency import apply_price_filter
from qlink_chatbot.utils.jr_search_index import ensure_product_search_indexes
from qlink_chatbot.utils.jr_search_sizes import (
    cm_to_ft_keyword_variants,
    parse_requested_cm_size,
    product_matches_cm_size,
)
from qlink_chatbot.utils.logger_config import logger

products_collection = db["products"]

MONGO_QUERY_MAX_MS = 25_000
CANDIDATE_LIMIT = 800

COMPACT_FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "color": ("search.color.single", "search.color.multi"),
    "material": ("search.material.primary", "search.material.family", "search.material.details"),
    "construction": ("search.construction",),
    "shape": ("search.shape",),
    "size": ("search.size.exact",),
    "pattern": ("search.style",),
    "room": ("search.room",),
}

PATTERN_RAW_FIELDS = (
    "raw.Pattern",
    "raw.StylePattern",
    "raw.Style",
    "raw.DecoreStyle",
)
COLOR_RAW_FIELDS = tuple(f"raw.{field}" for field in COLOR_GRBR_FIELDS)
COLOR_FAMILY_RAW_FIELDS = tuple(f"raw.{field}" for field in COLOR_FAMILY_FIELDS)


def size_regex(size_text: str) -> str:
    round_m = ROUND_SIZE_PATTERN.search(size_text.strip())
    if round_m:
        diameter = round_m.group(1)
        return rf"(?<!\d){diameter}\s*['′]?\s*round\b"
    m = SIZE_PATTERN.search(size_text.strip())
    if not m:
        return re.escape(size_text.strip())
    a, b = m.group(1), m.group(2)
    return rf"(?<!\d){a}\s*['′]?\s*[xX*]\s*{b}(?!\d)"


def classify_segment(segment: str) -> str:
    parts = [p.strip() for p in segment.split("||") if p.strip()]
    if not parts:
        return "general"

    lowered = [p.lower() for p in parts]
    seg_lower = segment.lower()

    if len(parts) == 1 and SIZE_PATTERN.search(parts[0]):
        return "size"
    if len(parts) == 1 and ROUND_SIZE_PATTERN.search(parts[0]):
        return "size"
    if len(parts) == 1 and parts[0].lower() in MULTICOLOR_KEYS:
        return "multicolor"
    if any(p.lower() in MULTICOLOR_KEYS for p in parts):
        return "multicolor"
    if all(p in KNOWN_SHAPE_VALUES for p in lowered):
        return "shape"
    if len(parts) == 1 and parts[0].lower() in KNOWN_SHAPE_VALUES:
        return "shape"

    color_hits = sum(1 for p in lowered if p in KNOWN_COLOR_VALUES)
    if color_hits == len(parts):
        return "color"
    if all(p in KNOWN_PATTERN_VALUES for p in lowered):
        return "pattern"

    for kw in sorted(CONSTRUCTION_KEYWORDS, key=len, reverse=True):
        if kw in seg_lower:
            return "construction"
    for kw in sorted(MATERIAL_KEYWORDS, key=len, reverse=True):
        if kw in seg_lower:
            return "material"
    for room in sorted(ROOM_KEYWORDS, key=len, reverse=True):
        if room in seg_lower:
            return "room"
    if WEIGHT_PATTERN.search(seg_lower):
        return "weight"
    if color_hits > 0:
        return "color"
    return "general"


def _segment_search_tokens(segment: str, segment_type: str) -> list[str]:
    parts = [p.strip().lower() for p in segment.split("||") if p.strip()]
    tokens: list[str] = []

    for part in parts:
        if segment_type == "size":
            round_match = ROUND_SIZE_PATTERN.search(part)
            if round_match:
                tokens.append(round_match.group(1))
                tokens.append("round")
            else:
                match = SIZE_PATTERN.search(part)
                if match:
                    tokens.append(f"{match.group(1)}x{match.group(2)}")
                else:
                    tokens.append(part)
        elif segment_type == "shape":
            tokens.append(SHAPE_ALIASES.get(part, part).lower())
        elif segment_type == "construction":
            for kw in sorted(CONSTRUCTION_KEYWORDS, key=len, reverse=True):
                if kw in part:
                    tokens.extend(kw.split())
                    break
            else:
                tokens.extend(part.split())
        elif segment_type == "weight":
            match = WEIGHT_PATTERN.search(part)
            if match:
                tokens.append(f"{match.group(1)}kg".replace(".0", ""))
        elif segment_type == "multicolor":
            tokens.extend(("multi", "multicolor", "multicolour"))
        else:
            tokens.append(part)

    return [t for t in tokens if t]


def _regex_or_clauses(field_names: tuple[str, ...], tokens: list[str]) -> list[dict]:
    clauses: list[dict] = []
    for field in field_names:
        for token in tokens:
            clauses.append({field: {"$regex": rf"\b{re.escape(token)}\b", "$options": "i"}})
    return clauses


def segment_to_mongo_clause(segment: str) -> dict:
    """Indexed token query with compact field fallback for older synced docs."""
    parts = [p.strip() for p in segment.split("||") if p.strip()]
    if not parts:
        return {}

    segment_type = classify_segment(segment)
    tokens = _segment_search_tokens(segment, segment_type)
    if segment_type == "color" and len(parts) == 1:
        tokens = color_search_terms(parts[0])
    if not tokens:
        return {}

    clauses: list[dict] = []

    if segment_type == "construction" and len(tokens) > 1:
        clauses.append({"search_tokens": {"$all": tokens}})
    elif segment_type == "size" and len(tokens) > 1:
        clauses.append({"search_tokens": {"$all": tokens}})
    elif segment_type == "multicolor":
        clauses.append({"search_tokens": {"$in": list(dict.fromkeys(tokens))}})
    elif segment_type == "color":
        unique_tokens = list(dict.fromkeys(tokens))
        clauses.append({"search_tokens": {"$in": unique_tokens}})
        clauses.extend(_regex_or_clauses(COLOR_RAW_FIELDS, unique_tokens))
        if not color_match_is_strict(set(unique_tokens)):
            clauses.extend(_regex_or_clauses(COLOR_FAMILY_RAW_FIELDS, unique_tokens))
    elif segment_type == "pattern":
        unique_tokens = list(dict.fromkeys(tokens))
        if len(unique_tokens) == 1:
            clauses.append({"search_tokens": unique_tokens[0]})
        else:
            clauses.append({"search_tokens": {"$in": unique_tokens}})
        clauses.extend(_regex_or_clauses(PATTERN_RAW_FIELDS, unique_tokens))
    elif len(tokens) == 1:
        clauses.append({"search_tokens": tokens[0]})
    else:
        clauses.extend({"search_tokens": token} for token in tokens)

    compact_fields = COMPACT_FIELDS_BY_TYPE.get(segment_type, ())
    regex_clauses = []
    for field in compact_fields:
        for token in tokens:
            regex_clauses.append({field: {"$regex": re.escape(token), "$options": "i"}})
    if regex_clauses and segment_type not in {"color", "pattern"}:
        clauses.append({"$or": regex_clauses})

    return {"$or": clauses} if len(clauses) > 1 else clauses[0]


def color_field_matches(term: str, field_value: str) -> bool:
    value = (field_value or "").lower().strip()
    if not value or not term:
        return False
    return bool(re.search(rf"\b{re.escape(term)}\b", value))


def product_matches_color_terms(product: dict, terms: set[str]) -> bool:
    """Match color on rug/border names; non-strict colors also use family/display fields."""
    fields = list(COLOR_GRBR_FIELDS)
    if not color_match_is_strict(terms):
        fields.extend(COLOR_FAMILY_FIELDS)
    for term in terms:
        for field in fields:
            if color_field_matches(term, str(product.get(field) or "")):
                return True
    return False


def product_matches_multicolor(product: dict) -> bool:
    color_family = str(product.get("ColorFamily") or "").lower().strip()
    if color_family == "multi":
        return True
    for field in ("DisplayFilter", "Pattern", "ColorFamily", "GrColor", "MultiFilter"):
        value = str(product.get(field) or "").lower()
        if re.search(r"\b(multi|multicolor|multicolour)\b", value):
            return True
    return False


def shape_field_matches(term: str, shape_value: str) -> bool:
    shape = (shape_value or "").lower().strip()
    if not shape or not term:
        return False
    catalog = SHAPE_ALIASES.get(term.lower(), term)
    return shape == catalog.lower() or term.lower() == shape


def part_matches_product(product: dict, part: str, segment_type: str) -> bool:
    part_lower = part.strip().lower()
    if not part_lower:
        return False

    if segment_type == "color":
        return product_matches_color_terms(product, set(color_search_terms(part)))

    if segment_type == "shape":
        return shape_field_matches(part_lower, str(product.get("Shape") or ""))

    if segment_type == "weight":
        weight_match = WEIGHT_PATTERN.search(part_lower)
        if not weight_match:
            return False
        max_kg = float(weight_match.group(1))
        try:
            weight = float(product.get("Weight") or 0)
        except (TypeError, ValueError):
            return False
        return 0 < weight <= max_kg

    if segment_type == "multicolor":
        return product_matches_multicolor(product)

    if segment_type == "room":
        return any(
            part_lower in str(product.get(field) or "").lower()
            for field in ("Room", "MultiFilter")
        )

    if segment_type == "general":
        return any(
            part_lower in str(product.get(field) or "").lower()
            for field in API_SEARCH_FIELDS
        )

    fields = [f.replace("raw.", "") for f in MONGO_FIELDS_BY_TYPE.get(segment_type, ())]
    if segment_type == "size":
        if parse_requested_cm_size(part):
            return product_matches_cm_size(product, part)
        for field in fields:
            val = str(product.get(field) or "")
            if re.search(size_regex(part), val, re.IGNORECASE):
                return True
        return False

    for field in fields:
        val = str(product.get(field) or "")
        if part_lower in val.lower():
            return True
    return False


def product_matches_segment(product: dict, segment: str) -> bool:
    parts = [p.strip() for p in segment.split("||") if p.strip()]
    if not parts:
        return True
    segment_type = classify_segment(segment)
    return any(part_matches_product(product, p, segment_type) for p in parts)


def filter_products_by_clean_keyword(products: list[dict], clean_keyword: str) -> list[dict]:
    segments = [s.strip() for s in clean_keyword.split("&") if s.strip()]
    if not segments:
        return products
    return [p for p in products if all(product_matches_segment(p, seg) for seg in segments)]


def get_instock_products(limit: int = CANDIDATE_LIMIT) -> list[dict]:
    ensure_product_search_indexes()
    cursor = products_collection.find(
        {"flags.inStock": True},
        {"_id": 0, "raw": 1},
        max_time_ms=MONGO_QUERY_MAX_MS,
    ).limit(limit)
    return [doc["raw"] for doc in cursor if doc.get("raw")]


def mongo_search_cm_products(size_cm_terms: set[str], candidate_limit: int = CANDIDATE_LIMIT) -> list[dict]:
    if not size_cm_terms:
        return []

    ensure_product_search_indexes()
    term = next(iter(size_cm_terms))
    target = parse_requested_cm_size(term)
    if not target:
        return []

    target_w, target_h = target
    for ft_keyword in cm_to_ft_keyword_variants(target_w, target_h):
        candidates = mongo_search_products(ft_keyword, candidate_limit=candidate_limit)
        matched = [p for p in candidates if product_matches_cm_size(p, term)]
        if matched:
            logger.info(
                f"[MONGO] cm size via ft keyword={ft_keyword!r} "
                f"→ {len(matched)} matched for {term!r}"
            )
            return matched[:candidate_limit]

    cursor = products_collection.find(
        {
            "flags.inStock": True,
            "raw.SizeInCM": {"$regex": r"\d", "$options": "i"},
        },
        {"_id": 0, "raw": 1},
        max_time_ms=MONGO_QUERY_MAX_MS,
    ).limit(2000)
    matched = [
        doc["raw"]
        for doc in cursor
        if doc.get("raw")
        and any(product_matches_cm_size(doc["raw"], size_term) for size_term in size_cm_terms)
    ]
    logger.info(
        f"[MONGO] cm size scan terms={size_cm_terms} → {len(matched)} matched"
    )
    return matched[:candidate_limit]


def mongo_search_products(clean_keyword: str, candidate_limit: int = CANDIDATE_LIMIT) -> list[dict]:
    if not clean_keyword:
        return []

    ensure_product_search_indexes()

    segments = [s.strip() for s in clean_keyword.split("&") if s.strip()]
    logger.info(
        f"[MONGO] search clean_keyword={clean_keyword!r} "
        f"segments={list(zip(segments, [classify_segment(s) for s in segments]))}"
    )

    and_clauses = [clause for seg in segments if (clause := segment_to_mongo_clause(seg))]
    query: dict = {"flags.inStock": True}
    if and_clauses:
        query["$and"] = and_clauses

    cursor = products_collection.find(
        query,
        {"_id": 0, "raw": 1},
        max_time_ms=MONGO_QUERY_MAX_MS,
    ).limit(candidate_limit)
    candidates = [doc["raw"] for doc in cursor if doc.get("raw")]
    results = filter_products_by_clean_keyword(candidates, clean_keyword)
    logger.info(f"[MONGO] {len(candidates)} candidates → {len(results)} matched")
    return results


def dedupe_by_sku(products: list[dict]) -> list[dict]:
    unique, seen = [], set()
    for p in products:
        key = str(p.get("SKU") or p.get("BarCode") or "").strip().upper()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(p)
    return unique


def apply_attribute_post_filters(products: list[dict], attribute_filters: dict[str, set]) -> list[dict]:
    result = products

    shape_terms = attribute_filters.get("shape") or set()
    if shape_terms:
        filtered = [
            p for p in result
            if any(shape_field_matches(t, str(p.get("Shape") or "")) for t in shape_terms)
        ]
        if filtered:
            logger.info(f"[SEARCH] shape filter: {len(result)} → {len(filtered)} (terms={shape_terms})")
            result = filtered
        else:
            return []

    weight_terms = attribute_filters.get("weight_max") or set()
    if weight_terms:
        max_kg = max(weight_terms)
        filtered = [p for p in result if 0 < float(p.get("Weight") or 0) <= max_kg]
        if filtered:
            result = filtered
        else:
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
            result = filtered
        else:
            return []

    size_cm_terms = attribute_filters.get("size_cm") or set()
    if size_cm_terms:
        filtered = [
            p for p in result
            if any(product_matches_cm_size(p, term) for term in size_cm_terms)
        ]
        if filtered:
            logger.info(
                f"[SEARCH] cm size filter: {len(result)} → {len(filtered)} "
                f"(terms={size_cm_terms})"
            )
            result = filtered
        else:
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
                        re.search(size_regex(term), str(p.get(field) or ""), re.IGNORECASE)
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
            result = filtered
        else:
            return []
    return result


def apply_search_pipeline(
    raw_results: list[dict],
    *,
    color_check_terms: set[str],
    attribute_filters: dict[str, set],
    price_filter: dict | None,
    exclude_skus: set | None,
) -> list[dict]:
    unique_results = dedupe_by_sku(raw_results)
    logger.info(f"[SEARCH] after dedup: {len(unique_results)} unique products")

    multicolor_terms = attribute_filters.get("multicolor") or set()
    if multicolor_terms:
        multicolor_filtered = [p for p in unique_results if product_matches_multicolor(p)]
        if multicolor_filtered:
            logger.info(
                f"[SEARCH] multicolor filter: {len(unique_results)} → {len(multicolor_filtered)} "
                f"(terms={multicolor_terms})"
            )
            unique_results = multicolor_filtered
        else:
            return []
    elif color_check_terms:
        color_filtered = [
            p for p in unique_results
            if product_matches_color_terms(p, color_check_terms)
        ]
        if color_filtered:
            logger.info(
                f"[SEARCH] color filter: {len(unique_results)} → {len(color_filtered)} "
                f"(terms={color_check_terms})"
            )
            unique_results = color_filtered
        else:
            return []

    unique_results = apply_attribute_post_filters(unique_results, attribute_filters)
    if not unique_results:
        return []

    if price_filter:
        unique_results = apply_price_filter(unique_results, price_filter)

    if exclude_skus:
        upper_exclude = {s.upper() for s in exclude_skus if s}
        unique_results = [
            p for p in unique_results
            if str(p.get("SKU", "")).upper() not in upper_exclude
        ]
    return unique_results
