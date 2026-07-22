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
    get_instock_products,
    mongo_search_cm_products,
    mongo_search_products,
    products_collection,
)
from qlink_chatbot.utils.jr_search_recommendation import (
    build_product_recommendation_reason,
    select_top_products,
)
from qlink_chatbot.utils.logger_config import logger

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
):
    """Search products from MongoDB (synced Product Master)."""
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

        unique_results, color_search_tier = await asyncio.to_thread(
            apply_search_pipeline,
            raw_results,
            color_check_terms=color_check_terms,
            attribute_filters=attribute_filters,
            price_filter=price_filter,
            exclude_skus=exclude_skus,
            skip_color_post_filter=color_prefiltered,
        )
        if not unique_results:
            logger.warning("[SEARCH] 0 products after filters")
            return {"error": "No products found."}

        if color_search_tier:
            logger.info(f"[SEARCH] color match tier: {color_search_tier}")

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
            limit=3,
        )
        logger.info(
            f"[SEARCH] selected {len(selected)} product(s) by color relevance "
            f"(pool={len(displayable)})"
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
                exact_color_terms=attribute_filters.get("color_exact") or set(),
                attribute_filters=attribute_filters,
                price_filter=price_filter,
                breakdown_by_sku=breakdown_by_sku,
                displayable_pool_size=len(displayable),
                rank=rank_idx,
                rank_score=rank_score,
            )
            recommendation_reasons.append(reason)
            logger.info(
                f"[SEARCH] recommend SKU={reason['SKU']!r} "
                f"method={reason['color_match_method']} "
                f"tier={reason['color_search_tier']!r} — {reason['summary']}"
            )

            formatted.append({
                "url": f"https://www.jaipurrugs.com/in/rugs/{p.get('ProductURL')}?barcode={barcode}",
                "price": {"currency": currency, "amount": price_amount},
                "display_currency": currency,
                "display_price": display_price,
                "price_source_field": currency_field,
                "color_search_tier": color_search_tier,
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
            for i in formatted
        ]
        logger.info(f"[SEARCH] final payload: {final_log}")
        logger.info(f"[SEARCH] recommendation detail: {recommendation_reasons}")
        logger.info(f"[SEARCH] returning {len(formatted)} product(s) for keyword={keyword!r}")
        return formatted

    except Exception as e:
        logger.error(f"[SEARCH] unexpected error: {e}")
        return {"error": f"Unexpected error: {str(e)}"}
