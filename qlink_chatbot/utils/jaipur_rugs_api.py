import asyncio

from qlink_chatbot.database.mongo_utils import db
from qlink_chatbot.utils.jr_search_currency import (
    CALLING_CODE_TO_CURRENCY,
    CURRENCY_FIELDS,
    build_display_price,
    extract_requested_currency_from_text,
    normalize_currency_code,
    resolve_currency_from_country_code,
    resolve_currency_from_ip,
)
from qlink_chatbot.utils.jr_search_color_breakdown import (
    load_breakdowns_for_skus,
    matched_breakdown_for_terms,
)
from qlink_chatbot.utils.jr_search_keywords import (
    normalise_keyword,
    normalise_search_keyword,
    resolve_search_keyword,
)
from qlink_chatbot.utils.jr_search_llm_extract import resolve_keyword_with_llm_extraction
from qlink_chatbot.utils.jr_search_mongo import (
    apply_search_pipeline,
    classify_segment,
    filters_without_size,
    get_instock_products,
    mongo_search_cm_products,
    mongo_search_products,
    products_collection,
    strip_size_segments_from_keyword,
)
from qlink_chatbot.utils.jr_search_recommendation import (
    build_product_recommendation_reason,
    select_top_products,
)
from qlink_chatbot.utils.logger_config import logger
from qlink_chatbot.utils.search_session import SEARCH_PAGE_SIZE, SEARCH_POOL_SIZE


def _copy_attribute_filters(attribute_filters: dict) -> dict:
    out = {}
    for key, values in (attribute_filters or {}).items():
        out[key] = set(values) if isinstance(values, (set, list, tuple)) else values
    return out


def _strip_values_from_keyword(keyword: str, values) -> str:
    drop = {str(v).strip().lower() for v in (values or []) if v}
    if not drop:
        return keyword or ""
    kept = [
        part.strip()
        for part in (keyword or "").split("&")
        if part.strip() and part.strip().lower() not in drop
    ]
    return "&".join(kept)


def _strip_segment_types_from_keyword(keyword: str, segment_types: set[str]) -> str:
    """Drop keyword &-segments whose classify_segment() is in segment_types."""
    if not keyword or not segment_types:
        return keyword or ""
    kept = [
        part.strip()
        for part in keyword.split("&")
        if part.strip() and classify_segment(part.strip()) not in segment_types
    ]
    return "&".join(kept)


# Kisna-style: drop secondary facets before giving up on an empty Mongo $and.
_MONGO_SEGMENT_DROP_ORDER: tuple[tuple[str, str], ...] = (
    ("size", "No exact size match in catalog — showing other sizes."),
    ("shape", "No rugs matched that shape with your other filters — showing other shapes."),
    ("room", "Broadened search by dropping the room filter."),
    ("pattern", "Broadened search by dropping the pattern filter."),
    ("construction", "Broadened search by dropping the construction filter."),
    ("material", "Broadened search by dropping the material filter."),
    ("catalog_tag", "Broadened search by dropping the catalog-tag filter."),
    ("weight", "Broadened search by dropping the weight filter."),
    ("general", "Broadened search — showing closer catalog matches."),
)

_SEGMENT_TYPE_TO_FILTER_KEYS: dict[str, tuple[str, ...]] = {
    "size": ("size", "size_cm", "size_category"),
    "shape": ("shape",),
    "room": ("room",),
    "pattern": ("pattern",),
    "construction": ("construction",),
    "material": ("material",),
    "catalog_tag": ("catalog_tag",),
    "weight": ("weight_max",),
}


def _widen_price_filter(price_filter: dict | None, factor: float = 1.25) -> dict | None:
    """Widen a budget band. Kisna drops price; we first widen, then drop.

    Critical: for $gte / min floors, widening must LOWER the threshold
    (amount / factor), not raise it.
    """
    if not price_filter:
        return None
    out = dict(price_filter)
    op = (out.get("operator") or "").strip()
    if "amount" in out and out["amount"] is not None:
        amount = float(out["amount"])
        if op == "$gte":
            out["amount"] = int(amount / factor)
        else:
            # $lte / default — raise the ceiling
            out["amount"] = int(amount * factor)
    if "max_amount" in out and out["max_amount"] is not None:
        out["max_amount"] = int(float(out["max_amount"]) * factor)
    if "min_amount" in out and out["min_amount"] is not None:
        out["min_amount"] = int(float(out["min_amount"]) / factor)
    return out


def _filters_without_keys(attribute_filters: dict, *keys: str) -> dict:
    out = _copy_attribute_filters(attribute_filters)
    for key in keys:
        if key in out and isinstance(out[key], set):
            out[key] = set()
        elif key in out:
            out[key] = None
    return out


# Progressive post-filter relax order (Kisna: drop one constraint at a time + honest note).
_PROGRESSIVE_RELAX_STEPS: tuple[tuple[str, str], ...] = (
    (
        "size_category",
        "No exact size-bucket match — showing other sizes that fit your other filters.",
    ),
    (
        "shape",
        "No rugs matched that shape with your other filters — showing other shapes.",
    ),
    ("room", "Broadened search by dropping the room filter."),
    ("weight_max", "Broadened search by dropping the weight filter."),
    ("catalog_tag", "Broadened search by dropping the catalog-tag filter."),
    ("pattern", "Broadened search by dropping the pattern filter."),
    ("construction", "Broadened search by dropping the construction filter."),
    ("material", "Broadened search by dropping the material filter."),
)


def _apply_drop_to_keyword(keyword: str, drop_key: str, values) -> str:
    """Strip keyword segments for a dropped attribute key."""
    if drop_key == "weight_max":
        return _strip_segment_types_from_keyword(keyword, {"weight"})
    if drop_key == "catalog_tag":
        kw = _strip_values_from_keyword(keyword, values)
        return _strip_segment_types_from_keyword(kw, {"catalog_tag"})
    if drop_key in {"size", "size_cm", "size_category"}:
        kw = _strip_values_from_keyword(keyword, values)
        return _strip_segment_types_from_keyword(kw, {"size"}) or kw
    if drop_key in {"shape", "room", "pattern", "construction", "material"}:
        kw = _strip_values_from_keyword(keyword, values)
        return _strip_segment_types_from_keyword(kw, {drop_key}) or kw
    return _strip_values_from_keyword(keyword, values)

# Kept for dashboard_routes imports
product_color_collection = db["product_color"]

__all__ = [
    "CALLING_CODE_TO_CURRENCY",
    "jaipur_rugs_product_search",
    "normalise_search_keyword",
    "products_collection",
    "resolve_search_keyword",
]


def _first_valid_image(p: dict) -> str:
    for key in ("HeadShot", "Corner", "CloseUp", "FoldShot", "Floorshot"):
        v = (p.get(key) or "").strip()
        if v and not v.endswith("/"):
            return v
    return ""


def _is_displayable_product(p: dict) -> bool:
    slug = (p.get("ProductURL") or "").strip()
    sku = (p.get("SKU") or p.get("BarCode") or "").strip()
    if not slug or not sku:
        return False
    return bool(_first_valid_image(p))


async def jaipur_rugs_product_search(
    keyword: str,
    client_ip: str = "",
    country_code: str = "",
    requested_currency: str = "",
    exclude_skus: set | None = None,
    user_message: str = "",
    previous_search_keyword: str = "",
    chat_context: str = "",
    skip_llm_extraction: bool = False,
    extraction_debug_out: list | None = None,
    pool_out: list | None = None,
    page_size: int | None = None,
    pool_size: int | None = None,
):
    """Search products from MongoDB (synced Product Master).

    Returns the first ``page_size`` products. When ``pool_out`` is provided,
    the full ranked pool (up to ``pool_size``) is appended for show-more buffer.
    """
    page_size = page_size if page_size is not None else SEARCH_PAGE_SIZE
    pool_size = pool_size if pool_size is not None else SEARCH_POOL_SIZE
    try:
        keyword = (keyword or "").strip()
        llm_extraction_debug = None
        if requested_currency:
            detected_currency = normalize_currency_code(requested_currency)
        else:
            detected_currency = resolve_currency_from_country_code(country_code)
            if not detected_currency:
                detected_currency = await resolve_currency_from_ip(client_ip)
            detected_currency = detected_currency or "INR"
        keyword, llm_extraction_debug = await resolve_keyword_with_llm_extraction(
            keyword,
            user_message=user_message,
            previous_search_keyword=previous_search_keyword,
            chat_context=chat_context,
            skip_llm_extraction=skip_llm_extraction,
            detected_currency=detected_currency,
            country_code=country_code,
        )
        if extraction_debug_out is not None and llm_extraction_debug is not None:
            extraction_debug_out.append(llm_extraction_debug)
        keyword = (keyword or "").strip()

        use_llm_payload = bool(
            llm_extraction_debug
            and llm_extraction_debug.get("use_llm_payload")
            and llm_extraction_debug.get("search_payload")
        )
        if use_llm_payload:
            payload = llm_extraction_debug["search_payload"]
            price_filter = payload.get("price_filter")
            clean_keyword = (payload.get("clean_keyword") or "").strip()
            color_check_terms = payload.get("color_check_terms") or set()
            attribute_filters = payload.get("attribute_filters") or {}
            norm_source = "llm_primary_payload"
        else:
            price_filter, clean_keyword, color_check_terms, attribute_filters = normalise_keyword(
                keyword
            )
            norm_source = "regex_normalise_keyword"

        requested_currency = (
            normalize_currency_code(requested_currency)
            if requested_currency
            else (price_filter or {}).get("currency")
            or extract_requested_currency_from_text(keyword)
            or detected_currency
        )
        logger.info(
            f"[SEARCH] start — keyword={keyword!r} "
            f"requested_currency={requested_currency!r} "
            f"country_code={country_code!r} client_ip={client_ip!r} "
            f"norm_source={norm_source}"
            + (f" llm_extraction={llm_extraction_debug}" if llm_extraction_debug else "")
        )
        logger.info(
            f"[SEARCH] normalised — clean_keyword={clean_keyword!r} "
            f"price_filter={price_filter} color_check_terms={color_check_terms} "
            f"attribute_filters={attribute_filters}"
        )

        if not clean_keyword and not price_filter and not any(attribute_filters.values()):
            logger.warning("[SEARCH] no searchable terms after normalisation")
            return {"error": "No products found."}

        color_prefiltered = False
        if not clean_keyword:
            logger.info("[SEARCH] price-only query — in-stock catalogue from MongoDB")
            raw_results = await asyncio.to_thread(get_instock_products)
            color_prefiltered = False
            search_source = "mongo-catalogue"
        else:
            raw_results, color_prefiltered = await asyncio.to_thread(
                mongo_search_products,
                clean_keyword,
            )
            search_source = "mongo-search"

        size_cm_terms = attribute_filters.get("size_cm") or set()
        if not raw_results and size_cm_terms:
            logger.info("[SEARCH] cm size token miss — scanning SizeInCM catalogue")
            raw_results = await asyncio.to_thread(mongo_search_cm_products, size_cm_terms)
            color_prefiltered = False
            search_source = "mongo-cm-size"

        if not raw_results and attribute_filters.get("multicolor"):
            logger.info("[SEARCH] multicolor token miss — scanning in-stock catalogue")
            raw_results = await asyncio.to_thread(get_instock_products)
            color_prefiltered = False
            search_source = "mongo-catalogue-multicolor"

        logger.info(f"[SEARCH] raw results from {search_source}: {len(raw_results)}")

        # Kisna-style: never give up on empty Mongo $and before dropping secondary segments.
        effective_filters = _copy_attribute_filters(attribute_filters)
        effective_keyword = clean_keyword
        effective_price = price_filter
        fallback_note = ""
        if not raw_results and clean_keyword and "&" in clean_keyword:
            working_segs = [s.strip() for s in clean_keyword.split("&") if s.strip()]
            working_filters = _copy_attribute_filters(attribute_filters)
            for drop_type, note in _MONGO_SEGMENT_DROP_ORDER:
                typed = [s for s in working_segs if classify_segment(s) == drop_type]
                if not typed:
                    continue
                # Never drop the last remaining color-bearing ask into empty keyword.
                remaining = [s for s in working_segs if classify_segment(s) != drop_type]
                if not remaining:
                    continue
                working_segs = remaining
                for filter_key in _SEGMENT_TYPE_TO_FILTER_KEYS.get(drop_type, ()):
                    working_filters = _filters_without_keys(working_filters, filter_key)
                if drop_type == "size":
                    working_filters = filters_without_size(working_filters)
                trial_kw = "&".join(working_segs)
                logger.info(
                    f"[SEARCH] empty Mongo $and — retry without segment_type={drop_type} "
                    f"keyword={trial_kw!r}"
                )
                trial_raw, trial_pre = await asyncio.to_thread(
                    mongo_search_products,
                    trial_kw,
                )
                if trial_raw:
                    raw_results = trial_raw
                    color_prefiltered = trial_pre
                    clean_keyword = trial_kw
                    effective_keyword = trial_kw
                    effective_filters = working_filters
                    attribute_filters = working_filters
                    fallback_note = note
                    search_source = f"mongo-search-relax-{drop_type}"
                    logger.info(
                        f"[SEARCH] recovered empty Mongo via drop={drop_type} "
                        f"n={len(raw_results)}"
                    )
                    break

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

        unique_results, color_search_tier, pipeline_meta = await asyncio.to_thread(
            apply_search_pipeline,
            raw_results,
            color_check_terms=color_check_terms,
            attribute_filters=attribute_filters,
            price_filter=price_filter,
            exclude_skus=exclude_skus,
            skip_color_post_filter=color_prefiltered,
        )
        size_relaxed = bool((pipeline_meta or {}).get("size_relaxed"))
        if size_relaxed and not fallback_note:
            fallback_note = (
                "No exact size match — showing closest available sizes that fit "
                "your other filters."
            )
            effective_filters = filters_without_size(effective_filters)

        # Prefer catalog-label color at nearby sizes over yarn-% matches at exact size.
        size_requested = bool(
            (attribute_filters.get("size") or set())
            or (attribute_filters.get("size_cm") or set())
            or (attribute_filters.get("size_category") or set())
        )
        weak_or_empty = (
            not unique_results
            or color_search_tier in {None, "breakdown_fallback"}
        )
        if size_requested and weak_or_empty and clean_keyword:
            kw_no_size = strip_size_segments_from_keyword(clean_keyword)
            has_size_bucket = bool(attribute_filters.get("size_category") or set())
            # Size-category-only queries have no ft/cm segment to strip — still re-run
            # without the size_category post-filter.
            should_relax_size = (
                (kw_no_size and kw_no_size != clean_keyword)
                or (has_size_bucket and not unique_results)
            )
            if should_relax_size:
                relax_keyword = (
                    kw_no_size
                    if (kw_no_size and kw_no_size != clean_keyword)
                    else clean_keyword
                )
                logger.info(
                    f"[SEARCH] re-query without size for catalog color "
                    f"(was tier={color_search_tier!r}) keyword={relax_keyword!r}"
                )
                raw_relaxed, color_pre_relaxed = await asyncio.to_thread(
                    mongo_search_products,
                    relax_keyword,
                )
                relaxed_filters = filters_without_size(attribute_filters)
                relaxed_results, relaxed_tier, relaxed_meta = await asyncio.to_thread(
                    apply_search_pipeline,
                    raw_relaxed,
                    color_check_terms=color_check_terms,
                    attribute_filters=relaxed_filters,
                    price_filter=price_filter,
                    exclude_skus=exclude_skus,
                    skip_color_post_filter=color_pre_relaxed,
                )
                prefer_relaxed = (
                    relaxed_results
                    and relaxed_tier in {
                        "exact_catalog_color",
                        "similar_catalog_color",
                        "mixture_catalog_color",
                    }
                )
                if prefer_relaxed or (not unique_results and relaxed_results):
                    unique_results = relaxed_results
                    color_search_tier = relaxed_tier
                    size_relaxed = True
                    effective_filters = relaxed_filters
                    effective_keyword = relax_keyword
                    if not fallback_note:
                        fallback_note = (
                            "No exact size match — showing closest available sizes "
                            "that fit your other filters."
                        )
                    logger.info(
                        f"[SEARCH] using size-relaxed catalog results "
                        f"tier={color_search_tier!r} n={len(unique_results)}"
                    )

        # Progressive filter relax (Kisna: drop one constraint + honest note).
        if not unique_results:
            working_filters = _copy_attribute_filters(attribute_filters)
            working_keyword = clean_keyword
            working_price = price_filter
            for drop_key, note in _PROGRESSIVE_RELAX_STEPS:
                values = working_filters.get(drop_key) or set()
                if not values:
                    continue
                working_filters = _filters_without_keys(working_filters, drop_key)
                working_keyword = _apply_drop_to_keyword(
                    working_keyword, drop_key, values
                )
                if working_keyword:
                    raw_fb, color_pre_fb = await asyncio.to_thread(
                        mongo_search_products,
                        working_keyword,
                    )
                else:
                    raw_fb = raw_results
                    color_pre_fb = color_prefiltered
                fb_results, fb_tier, _fb_meta = await asyncio.to_thread(
                    apply_search_pipeline,
                    raw_fb,
                    color_check_terms=color_check_terms,
                    attribute_filters=working_filters,
                    price_filter=working_price,
                    exclude_skus=exclude_skus,
                    skip_color_post_filter=color_pre_fb,
                )
                if fb_results:
                    unique_results = fb_results
                    color_search_tier = fb_tier
                    fallback_note = note
                    effective_filters = working_filters
                    effective_keyword = working_keyword
                    effective_price = working_price
                    if drop_key == "size_category":
                        size_relaxed = True
                    logger.info(
                        f"[SEARCH] progressive fallback dropped={drop_key} "
                        f"n={len(unique_results)}"
                    )
                    break

            if not unique_results and working_price:
                widened = _widen_price_filter(working_price, 1.25)
                if working_keyword:
                    raw_fb, color_pre_fb = await asyncio.to_thread(
                        mongo_search_products,
                        working_keyword,
                    )
                else:
                    raw_fb = raw_results
                    color_pre_fb = color_prefiltered
                fb_results, fb_tier, _fb_meta = await asyncio.to_thread(
                    apply_search_pipeline,
                    raw_fb,
                    color_check_terms=color_check_terms,
                    attribute_filters=working_filters,
                    price_filter=widened,
                    exclude_skus=exclude_skus,
                    skip_color_post_filter=color_pre_fb,
                )
                if fb_results:
                    unique_results = fb_results
                    color_search_tier = fb_tier
                    fallback_note = (
                        "No exact budget match — showing a slightly wider price range."
                    )
                    effective_filters = working_filters
                    effective_keyword = working_keyword
                    effective_price = widened
                    logger.info(
                        f"[SEARCH] progressive fallback widened price n={len(unique_results)}"
                    )

            # Kisna last resort: drop price entirely while keeping remaining filters.
            if not unique_results and working_price:
                if working_keyword:
                    raw_fb, color_pre_fb = await asyncio.to_thread(
                        mongo_search_products,
                        working_keyword,
                    )
                else:
                    raw_fb = raw_results
                    color_pre_fb = color_prefiltered
                fb_results, fb_tier, _fb_meta = await asyncio.to_thread(
                    apply_search_pipeline,
                    raw_fb,
                    color_check_terms=color_check_terms,
                    attribute_filters=working_filters,
                    price_filter=None,
                    exclude_skus=exclude_skus,
                    skip_color_post_filter=color_pre_fb,
                )
                if fb_results:
                    unique_results = fb_results
                    color_search_tier = fb_tier
                    fallback_note = (
                        "No rugs in that exact price range — showing closest matches "
                        "with your other filters."
                    )
                    effective_filters = working_filters
                    effective_keyword = working_keyword
                    effective_price = None
                    logger.info(
                        f"[SEARCH] progressive fallback dropped price n={len(unique_results)}"
                    )

        if not unique_results:
            logger.warning("[SEARCH] 0 products after filters")
            return {"error": "No products found."}

        if color_search_tier:
            logger.info(
                f"[SEARCH] color match tier: {color_search_tier}"
                + (" size_relaxed=true" if size_relaxed else "")
            )

        displayable = [p for p in unique_results if _is_displayable_product(p)]
        if not displayable:
            logger.warning("[SEARCH] 0 displayable products after URL/image validation")
            return {"error": "No products found."}
        if len(displayable) < len(unique_results):
            logger.info(
                f"[SEARCH] displayable filter: {len(unique_results)} → {len(displayable)}"
            )

        match_terms = attribute_filters.get("color_exact") or color_check_terms
        displayable_skus = [
            str(p.get("SKU") or p.get("BarCode") or "").strip()
            for p in displayable
        ]
        breakdown_by_sku = await asyncio.to_thread(
            load_breakdowns_for_skus,
            [s for s in displayable_skus if s],
        )
        selected, rank_scores = select_top_products(
            displayable,
            match_terms=set(match_terms) if match_terms else set(),
            exact_color_terms=attribute_filters.get("color_exact") or set(),
            breakdown_by_sku=breakdown_by_sku,
            color_search_tier=color_search_tier,
            size_terms=attribute_filters.get("size") or set(),
            size_relaxed=size_relaxed,
            limit=pool_size,
        )
        logger.info(
            f"[SEARCH] selected {len(selected)} product(s) by color relevance "
            f"(pool={len(displayable)} page_size={page_size})"
        )

        currency = requested_currency or ""
        if not currency and price_filter:
            currency = price_filter.get("currency", "")
        if not currency:
            currency = resolve_currency_from_country_code(country_code)
        if not currency:
            currency = await resolve_currency_from_ip(client_ip)
        currency_field = CURRENCY_FIELDS.get(currency, "INR_MRP")
        logger.info(
            f"[CURRENCY] requested={requested_currency!r} "
            f"resolved={currency!r} field={currency_field}"
        )

        formatted = []
        recommendation_reasons = []
        for rank_idx, (p, rank_score) in enumerate(zip(selected, rank_scores), start=1):
            sku = str(p.get("SKU") or p.get("BarCode") or "").strip()
            barcode = str(p.get("BarCode") or "").strip()
            price_amount = p.get(currency_field)
            display_price = build_display_price(currency, price_amount)
            matched_color = matched_breakdown_for_terms(
                p,
                set(match_terms) if match_terms else set(),
                breakdown_by_sku,
            )
            reason = build_product_recommendation_reason(
                p,
                color_search_tier=color_search_tier,
                match_terms=set(match_terms) if match_terms else set(),
                exact_color_terms=effective_filters.get("color_exact") or set(),
                attribute_filters=effective_filters,
                price_filter=effective_price,
                breakdown_by_sku=breakdown_by_sku,
                displayable_pool_size=len(displayable),
                rank=rank_idx,
                rank_score=rank_score,
                size_relaxed=size_relaxed,
            )
            recommendation_reasons.append(reason)
            logger.info(
                f"[SEARCH] recommend SKU={reason['SKU']!r} "
                f"method={reason['color_match_method']} "
                f"tier={reason['color_search_tier']!r} — {reason['summary']}"
            )

            if fallback_note:
                reason = {**reason, "fallback_note": fallback_note}
            formatted.append({
                "url": f"https://www.jaipurrugs.com/in/rugs/{p.get('ProductURL')}?barcode={barcode}",
                "price": {"currency": currency, "amount": price_amount},
                "display_currency": currency,
                "display_price": display_price,
                "price_source_field": currency_field,
                "color_search_tier": color_search_tier,
                "size_relaxed": size_relaxed,
                "fallback_note": fallback_note,
                "name": (p.get("Name") or p.get("Collection") or "").strip(),
                "SKU": sku,
                "collection": p.get("Collection", ""),
                "size": p.get("SizeInFT", ""),
                "size_cm": p.get("SizeInCM", ""),
                "shape": p.get("Shape", ""),
                "color": p.get("GrColor", ""),
                "border_color": p.get("BrColor", ""),
                "color_family": p.get("ColorFamily", ""),
                "display_filter": p.get("DisplayFilter", ""),
                "color_mood": p.get("ColorMood", ""),
                "pattern": p.get("Pattern", ""),
                "matched_color_percentage": matched_color,
                "recommendation_reason": reason,
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

        if pool_out is not None:
            pool_out.extend(formatted)

        page = formatted[: max(1, page_size)]

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
                "color_match_method": i["recommendation_reason"]["color_match_method"],
                "color_search_tier": i["recommendation_reason"]["color_search_tier"],
                "breakdown_matched": i["recommendation_reason"]["breakdown_matched_percentages"],
                "why_recommended": i["recommendation_reason"]["summary"],
            }
            for i in page
        ]
        logger.info(f"[SEARCH] final payload: {final_log}")
        logger.info(f"[SEARCH] recommendation detail: {recommendation_reasons[:len(page)]}")
        logger.info(
            f"[SEARCH] returning {len(page)} product(s) "
            f"(pool={len(formatted)}) for keyword={keyword!r}"
        )
        return page

    except Exception as e:
        logger.error(f"[SEARCH] unexpected error: {e}")
        return {"error": f"Unexpected error: {str(e)}"}
