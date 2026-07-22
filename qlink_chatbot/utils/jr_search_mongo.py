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
    COLOR_MATCH_FIELDS,
)
from qlink_chatbot.utils.jr_search_color_breakdown import (
    breakdown_colors_index_ready,
    ensure_breakdown_colors_backfilled,
    instock_skus_for_breakdown_colors,
    load_breakdowns_for_skus,
    product_matches_color_breakdown,
    user_terms_to_breakdown_colors,
)
from qlink_chatbot.utils.jr_search_currency import apply_price_filter
from qlink_chatbot.utils.jr_search_index import ensure_product_search_indexes
from qlink_chatbot.utils.jr_search_sizes import (
    cm_to_ft_keyword_variants,
    parse_requested_cm_size,
    product_matches_cm_size,
    product_matches_size_category,
)
from qlink_chatbot.utils.logger_config import logger

products_collection = db["products"]

MONGO_QUERY_MAX_MS = 25_000
CANDIDATE_LIMIT = 800
COLOR_SAMPLE_SIZE = 120

LISTING_RAW_FIELDS: tuple[str, ...] = (
    "SKU", "BarCode", "Name", "Collection", "ProductURL", "SizeInFT", "SizeInCM", "Shape",
    "GrColor", "BrColor", "ColorFamily", "DisplayFilter", "ColorMood", "Pattern", "Style",
    "DecoreStyle", "StylePattern", "MultiFilter",
    "Construction", "Material", "MaterialDetails", "MaterialFamilies", "Quality", "Room",
    "Weight", "HeadShot", "Corner", "CloseUp", "FoldShot", "Floorshot",
    "INR_MRP", "USD_MRP", "EUR_MRP", "GBP_MRP", "AUD_MRP", "CHF_MRP", "SGD_MRP", "AED_MRP",
)


def listing_projection() -> dict:
    return {"_id": 0, **{f"raw.{field}": 1 for field in LISTING_RAW_FIELDS}}


def docs_to_raw_products(docs: list[dict]) -> list[dict]:
    products: list[dict] = []
    for doc in docs:
        raw = doc.get("raw")
        if isinstance(raw, dict):
            products.append(raw)
    return products

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
COLOR_RAW_FIELDS = tuple(f"raw.{field}" for field in COLOR_MATCH_FIELDS)
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


def keyword_has_mixture_colors(clean_keyword: str) -> bool:
    """True when keyword has multicolor or 2+ color segments (e.g. pink&purple)."""
    segments = [s.strip() for s in (clean_keyword or "").split("&") if s.strip()]
    if any(classify_segment(seg) == "multicolor" for seg in segments):
        return True
    color_segments = [seg for seg in segments if classify_segment(seg) == "color"]
    return len(color_segments) >= 2


def segment_to_mongo_clause(segment: str, *, mixture_mode: bool = False) -> dict:
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
        # Catalog-first recall (website-style labels). Yarn % / breakdown_colors
        # is applied later as a weaker post-filter fallback, not the Mongo gate.
        unique_tokens = list(dict.fromkeys(tokens))
        clauses.append({"search_tokens": {"$in": unique_tokens}})
        clauses.extend(_regex_or_clauses(COLOR_RAW_FIELDS, unique_tokens))
        clauses.extend(_regex_or_clauses(("raw.DisplayFilter",), unique_tokens))
        # ColorFamily: mixture intent (pink&purple) and non-strict palettes.
        # Strict single pink/red keeps ColorFamily out so "Pink and Purple" isn't recalled.
        if mixture_mode or not color_match_is_strict(set(unique_tokens)):
            clauses.extend(_regex_or_clauses(COLOR_FAMILY_RAW_FIELDS, unique_tokens))
        logger.info(
            f"[MONGO] color catalog clause tokens={unique_tokens} "
            f"mixture_mode={mixture_mode} "
            "(GrColor/DisplayFilter/tokens; breakdown not used for recall)"
        )
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


def product_matches_exact_grcolor_terms(product: dict, terms: set[str]) -> bool:
    """Match only the user's exact color word(s) on ground color (GrColor)."""
    for term in terms:
        if color_field_matches(term, str(product.get("GrColor") or "")):
            return True
    return False


def product_matches_catalog_color(product: dict, terms: set[str]) -> bool:
    """Strong single-color match: GrColor or DisplayFilter (not ColorFamily mixtures)."""
    for term in terms:
        if color_field_matches(term, str(product.get("GrColor") or "")):
            return True
        if color_field_matches(term, str(product.get("DisplayFilter") or "")):
            return True
    return False


def product_matches_color_family_terms(product: dict, terms: set[str]) -> bool:
    """Weaker catalog match via ColorFamily / ColorMood / BasicColor."""
    for term in terms:
        for field in COLOR_FAMILY_FIELDS:
            if color_field_matches(term, str(product.get(field) or "")):
                return True
    return False


def product_matches_single_color_family(product: dict, terms: set[str]) -> bool:
    """ColorFamily hit for single-color intent — reject mixture families like 'Pink and Purple'."""
    family = str(product.get("ColorFamily") or "").strip()
    if re.search(r"\band\b", family, re.IGNORECASE):
        return False
    return product_matches_color_family_terms(product, terms)


def product_matches_color_terms(
    product: dict,
    terms: set[str],
    *,
    mixture_mode: bool = False,
) -> bool:
    """Match on ground color (GrColor/DisplayFilter) and, for broad/mixture, ColorFamily."""
    if product_matches_catalog_color(product, terms):
        return True
    if mixture_mode or not color_match_is_strict(terms):
        return product_matches_color_family_terms(product, terms)
    return False


def product_matches_mixture_colors(
    product: dict,
    exact_color_terms: set[str],
    *,
    breakdown_by_sku: dict[str, list[dict]] | None = None,
) -> bool:
    """Require evidence for each exact color (catalog label, family, or yarn %)."""
    if not exact_color_terms:
        return False
    for exact in exact_color_terms:
        terms = set(color_search_terms(exact))
        if product_matches_catalog_color(product, terms):
            continue
        if product_matches_color_family_terms(product, terms):
            continue
        breakdown_colors = user_terms_to_breakdown_colors(terms)
        if breakdown_colors and product_matches_color_breakdown(
            product, breakdown_colors, breakdown_by_sku or {},
        ):
            continue
        return False
    return True


def is_mixture_color_intent(attribute_filters: dict[str, set]) -> bool:
    """True when user asked for multicolor or 2+ exact colors (e.g. pink&purple)."""
    if attribute_filters.get("multicolor"):
        return True
    exact = attribute_filters.get("color_exact") or set()
    return len(exact) >= 2


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


def part_matches_product(
    product: dict,
    part: str,
    segment_type: str,
    *,
    mixture_mode: bool = False,
) -> bool:
    part_lower = part.strip().lower()
    if not part_lower:
        return False

    if segment_type == "color":
        terms = set(color_search_terms(part))
        # Catalog-first for candidate filtering; breakdown is a pipeline fallback.
        return product_matches_color_terms(product, terms, mixture_mode=mixture_mode)

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


def product_matches_segment(
    product: dict,
    segment: str,
    *,
    skip_color_check: bool = False,
    mixture_mode: bool = False,
) -> bool:
    parts = [p.strip() for p in segment.split("||") if p.strip()]
    if not parts:
        return True
    segment_type = classify_segment(segment)
    if skip_color_check and segment_type == "color":
        return True
    return any(
        part_matches_product(product, p, segment_type, mixture_mode=mixture_mode)
        for p in parts
    )


def filter_products_by_clean_keyword(
    products: list[dict],
    clean_keyword: str,
    *,
    skip_color_check: bool = False,
) -> list[dict]:
    segments = [s.strip() for s in clean_keyword.split("&") if s.strip()]
    if not segments:
        return products
    mixture_mode = keyword_has_mixture_colors(clean_keyword)
    return [
        p for p in products
        if all(
            product_matches_segment(
                p,
                seg,
                skip_color_check=skip_color_check,
                mixture_mode=mixture_mode,
            )
            for seg in segments
        )
    ]


def segment_uses_breakdown_clause(segment: str) -> bool:
    """Deprecated for primary recall — catalog-first search never prefilters by yarn %."""
    return False


def breakdown_colors_for_keyword(clean_keyword: str) -> set[str]:
    colors: set[str] = set()
    for segment in [s.strip() for s in clean_keyword.split("&") if s.strip()]:
        if classify_segment(segment) != "color":
            continue
        parts = [p.strip() for p in segment.split("||") if p.strip()]
        if not parts:
            continue
        tokens = color_search_terms(parts[0]) if len(parts) == 1 else []
        colors.update(user_terms_to_breakdown_colors(set(tokens)))
    return colors


def sample_breakdown_color_products(
    breakdown_colors: set[str],
    sample_size: int = COLOR_SAMPLE_SIZE,
) -> list[dict]:
    ensure_breakdown_colors_backfilled()
    if not breakdown_colors or not breakdown_colors_index_ready():
        return []

    pipeline = [
        {
            "$match": {
                "flags.inStock": True,
                "breakdown_colors": {"$in": list(breakdown_colors)},
            },
        },
        {"$sample": {"size": sample_size}},
        {"$project": listing_projection()},
    ]
    docs = list(products_collection.aggregate(pipeline, maxTimeMS=MONGO_QUERY_MAX_MS))
    return docs_to_raw_products(docs)


def fetch_listing_products(query: dict, limit: int) -> list[dict]:
    cursor = products_collection.find(
        query,
        listing_projection(),
        max_time_ms=MONGO_QUERY_MAX_MS,
    ).limit(limit)
    return docs_to_raw_products(list(cursor))


def get_instock_products(limit: int = CANDIDATE_LIMIT) -> list[dict]:
    ensure_product_search_indexes()
    return fetch_listing_products({"flags.inStock": True}, limit)


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
        candidates, _ = mongo_search_products(ft_keyword, candidate_limit=candidate_limit)
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
        listing_projection(),
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


def strip_size_segments_from_keyword(clean_keyword: str) -> str:
    """Drop ft/cm size segments so color recall can widen beyond exact size."""
    kept: list[str] = []
    for segment in [s.strip() for s in (clean_keyword or "").split("&") if s.strip()]:
        if classify_segment(segment) == "size":
            continue
        kept.append(segment)
    return "&".join(kept)


def filters_without_size(attribute_filters: dict[str, set]) -> dict[str, set]:
    out: dict[str, set] = {}
    for key, values in (attribute_filters or {}).items():
        if key in {"size", "size_cm", "size_category"}:
            continue
        out[key] = set(values) if values else set()
    return out


def mongo_search_products(
    clean_keyword: str,
    candidate_limit: int = CANDIDATE_LIMIT,
) -> tuple[list[dict], bool]:
    if not clean_keyword:
        return [], False

    ensure_product_search_indexes()

    segments = [s.strip() for s in clean_keyword.split("&") if s.strip()]
    logger.info(
        f"[MONGO] search clean_keyword={clean_keyword!r} "
        f"segments={list(zip(segments, [classify_segment(s) for s in segments]))}"
    )

    breakdown_prefiltered = any(segment_uses_breakdown_clause(seg) for seg in segments)
    only_breakdown_colors = (
        breakdown_prefiltered
        and all(
            classify_segment(seg) == "color" and segment_uses_breakdown_clause(seg)
            for seg in segments
        )
    )

    if only_breakdown_colors and breakdown_colors_index_ready():
        breakdown_colors = breakdown_colors_for_keyword(clean_keyword)
        results = sample_breakdown_color_products(breakdown_colors)
        logger.info(
            f"[MONGO] color sample search colors={sorted(breakdown_colors)} "
            f"→ {len(results)} products"
        )
        return results, True

    mixture_mode = keyword_has_mixture_colors(clean_keyword)
    and_clauses = [
        clause
        for seg in segments
        if (clause := segment_to_mongo_clause(seg, mixture_mode=mixture_mode))
    ]
    query: dict = {"flags.inStock": True}
    if and_clauses:
        query["$and"] = and_clauses

    candidates = fetch_listing_products(query, candidate_limit)
    if only_breakdown_colors:
        results = candidates
    else:
        results = filter_products_by_clean_keyword(
            candidates,
            clean_keyword,
            skip_color_check=breakdown_prefiltered,
        )
    logger.info(
        f"[MONGO] {len(candidates)} candidates → {len(results)} matched "
        f"(mixture_mode={mixture_mode})"
    )
    return results, breakdown_prefiltered


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


def _filter_by_size_ft(products: list[dict], size_terms: set) -> list[dict]:
    filtered = []
    for p in products:
        for term in size_terms:
            if any(
                re.search(size_regex(str(term)), str(p.get(field) or ""), re.IGNORECASE)
                for field in ("SizeInFT", "SizeInCM")
            ):
                filtered.append(p)
                break
    return filtered


def apply_attribute_post_filters(
    products: list[dict],
    attribute_filters: dict[str, set],
    *,
    apply_size: bool = True,
    hard_size: bool = True,
) -> list[dict]:
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

    size_category_terms = attribute_filters.get("size_category") or set()
    if size_category_terms:
        filtered = [
            p for p in result
            if any(product_matches_size_category(p, term) for term in size_category_terms)
        ]
        if filtered:
            logger.info(
                f"[SEARCH] size_category filter: {len(result)} → {len(filtered)} "
                f"(terms={size_category_terms})"
            )
            result = filtered
        else:
            return []

    if apply_size:
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
            elif hard_size:
                logger.info(
                    f"[SEARCH] cm size filter matched 0 — hard empty (terms={size_cm_terms})"
                )
                return []

        size_terms = attribute_filters.get("size") or set()
        if size_terms:
            filtered = _filter_by_size_ft(result, size_terms)
            if filtered:
                logger.info(
                    f"[SEARCH] size filter: {len(result)} → {len(filtered)} (terms={size_terms})"
                )
                result = filtered
            elif hard_size:
                logger.info(
                    f"[SEARCH] size filter matched 0 — hard empty (terms={size_terms})"
                )
                return []
            else:
                logger.info(
                    f"[SEARCH] size filter matched 0 — keeping {len(result)} prior results "
                    f"(terms={size_terms})"
                )

    for attr, fields in (
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
                term_l = str(term).lower().replace("-", " ")
                for field in fields:
                    field_l = str(p.get(field) or "").lower().replace("-", " ")
                    if term_l and term_l in field_l:
                        matched = True
                        break
                if matched:
                    break
            if matched:
                filtered.append(p)
        if filtered:
            logger.info(
                f"[SEARCH] {attr} filter: {len(result)} → {len(filtered)} (terms={terms})"
            )
            result = filtered
        else:
            logger.info(
                f"[SEARCH] {attr} filter matched 0 — keeping {len(result)} prior results "
                f"(terms={terms})"
            )
    return result


def apply_search_pipeline(
    raw_results: list[dict],
    *,
    color_check_terms: set[str],
    attribute_filters: dict[str, set],
    price_filter: dict | None,
    exclude_skus: set | None,
    skip_color_post_filter: bool = False,
) -> tuple[list[dict], str | None, dict]:
    """Apply color then attribute filters.

    Returns (products, color_search_tier, meta) where meta may include size_relaxed.
    """
    unique_results = dedupe_by_sku(raw_results)
    logger.info(f"[SEARCH] after dedup: {len(unique_results)} unique products")
    color_search_tier: str | None = None
    meta: dict = {"size_relaxed": False}

    multicolor_terms = attribute_filters.get("multicolor") or set()
    exact_color_terms = set(attribute_filters.get("color_exact") or set())
    match_terms = set(color_check_terms or set())
    for term in exact_color_terms:
        match_terms.update(color_search_terms(term))
    if not match_terms and exact_color_terms:
        match_terms = set(exact_color_terms)

    mixture = is_mixture_color_intent(attribute_filters)

    if multicolor_terms:
        multicolor_filtered = [p for p in unique_results if product_matches_multicolor(p)]
        if multicolor_filtered:
            logger.info(
                f"[SEARCH] multicolor filter: {len(unique_results)} → {len(multicolor_filtered)} "
                f"(terms={multicolor_terms})"
            )
            unique_results = multicolor_filtered
            color_search_tier = "multicolor"
        else:
            return [], None, meta
    elif exact_color_terms or color_check_terms:
        if skip_color_post_filter:
            # Legacy path when Mongo already prefiltered — still label as catalog.
            color_search_tier = "exact_catalog_color"
        elif mixture:
            # Mixture: each exact color must appear via catalog/family or yarn %.
            skus = [
                str(p.get("SKU") or p.get("BarCode") or "").strip()
                for p in unique_results
            ]
            breakdown_by_sku = load_breakdowns_for_skus([s for s in skus if s])
            required_colors = exact_color_terms or match_terms
            mixture_filtered = [
                p for p in unique_results
                if product_matches_mixture_colors(
                    p,
                    required_colors,
                    breakdown_by_sku=breakdown_by_sku,
                )
            ]
            if mixture_filtered:
                logger.info(
                    f"[SEARCH] mixture color filter: {len(unique_results)} → "
                    f"{len(mixture_filtered)} (terms={sorted(required_colors)})"
                )
                unique_results = mixture_filtered
                color_search_tier = "mixture_catalog_color"
            else:
                return [], None, meta
        else:
            # Single-color: catalog labels first, yarn % only as last fallback.
            catalog_exact = [
                p for p in unique_results
                if product_matches_catalog_color(p, match_terms)
            ]
            if catalog_exact:
                logger.info(
                    f"[SEARCH] exact catalog color filter: {len(unique_results)} → "
                    f"{len(catalog_exact)} (terms={sorted(match_terms)})"
                )
                unique_results = catalog_exact
                color_search_tier = "exact_catalog_color"
            else:
                family_filtered = [
                    p for p in unique_results
                    if product_matches_single_color_family(p, match_terms)
                ]
                if family_filtered:
                    logger.info(
                        f"[SEARCH] similar catalog color (non-mixture ColorFamily) filter: "
                        f"{len(unique_results)} → {len(family_filtered)} "
                        f"(terms={sorted(match_terms)})"
                    )
                    unique_results = family_filtered
                    color_search_tier = "similar_catalog_color"
                else:
                    skus = [
                        str(p.get("SKU") or p.get("BarCode") or "").strip()
                        for p in unique_results
                    ]
                    breakdown_by_sku = load_breakdowns_for_skus([s for s in skus if s])
                    breakdown_colors = user_terms_to_breakdown_colors(
                        exact_color_terms or match_terms
                    )
                    breakdown_filtered = [
                        p for p in unique_results
                        if breakdown_colors
                        and product_matches_color_breakdown(
                            p, breakdown_colors, breakdown_by_sku,
                        )
                    ] if breakdown_colors else []
                    if breakdown_filtered:
                        logger.info(
                            f"[SEARCH] breakdown fallback filter: {len(unique_results)} → "
                            f"{len(breakdown_filtered)} (colors={sorted(breakdown_colors)})"
                        )
                        unique_results = breakdown_filtered
                        color_search_tier = "breakdown_fallback"
                    else:
                        return [], None, meta

    # Non-size attrs first (hard size applied next with optional relax).
    color_pool = unique_results
    unique_results = apply_attribute_post_filters(
        color_pool,
        attribute_filters,
        apply_size=True,
        hard_size=True,
    )
    size_terms = attribute_filters.get("size") or set()
    size_cm_terms = attribute_filters.get("size_cm") or set()
    if (
        not unique_results
        and color_pool
        and (size_terms or size_cm_terms)
    ):
        # Soft-fallback: keep color quality, drop exact size requirement.
        logger.info(
            "[SEARCH] size hard-empty after color filter — relaxing size "
            f"(size={sorted(size_terms)} size_cm={sorted(size_cm_terms)})"
        )
        unique_results = apply_attribute_post_filters(
            color_pool,
            attribute_filters,
            apply_size=False,
            hard_size=True,
        )
        meta["size_relaxed"] = bool(unique_results)

    if not unique_results:
        return [], None, meta

    if price_filter:
        unique_results = apply_price_filter(unique_results, price_filter)

    if exclude_skus:
        upper_exclude = {s.upper() for s in exclude_skus if s}
        unique_results = [
            p for p in unique_results
            if str(p.get("SKU", "")).upper() not in upper_exclude
        ]
    return unique_results, color_search_tier, meta
