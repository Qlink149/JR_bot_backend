import asyncio
import random

from qlink_chatbot.database.mongo_utils import db
from qlink_chatbot.utils.jr_api_client import search_products as _jr_search_products
from qlink_chatbot.utils.jr_search_currency import (
    CALLING_CODE_TO_CURRENCY,
    CURRENCY_FIELDS,
    build_display_price,
    extract_requested_currency_from_text,
    normalize_currency_code,
    resolve_currency_from_country_code,
    resolve_currency_from_ip,
)
from qlink_chatbot.utils.jr_search_keywords import (
    normalise_keyword,
    normalise_search_keyword,
    resolve_search_keyword,
)
from qlink_chatbot.utils.jr_search_mongo import (
    apply_search_pipeline,
    get_instock_products,
    mongo_search_products,
    products_collection,
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


async def _mongo_search_safe(clean_keyword: str) -> list[dict]:
    try:
        return await asyncio.to_thread(mongo_search_products, clean_keyword)
    except Exception as err:
        logger.warning(f"[SEARCH] MongoDB search failed — will try API fallback: {err}")
        return []


async def _mongo_instock_safe() -> list[dict]:
    try:
        return await asyncio.to_thread(get_instock_products)
    except Exception as err:
        logger.warning(f"[SEARCH] MongoDB in-stock fetch failed — will try API fallback: {err}")
        return []


async def _api_search_safe(clean_keyword: str) -> list[dict]:
    try:
        return await _jr_search_products(clean_keyword)
    except Exception as err:
        logger.warning(f"[SEARCH] JR API search failed: {err}")
        return []


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
            normalize_currency_code(requested_currency)
            if requested_currency
            else extract_requested_currency_from_text(keyword)
        )
        logger.info(
            f"[SEARCH] start — keyword={keyword!r} "
            f"requested_currency={requested_currency!r} "
            f"country_code={country_code!r} client_ip={client_ip!r}"
        )

        price_filter, clean_keyword, color_check_terms, attribute_filters = normalise_keyword(keyword)
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
            raw_results = await _mongo_instock_safe()
            search_source = "mongo-catalogue"
            if not raw_results:
                from qlink_chatbot.utils.jr_api_client import get_all_products as _jr_all
                try:
                    raw_results = await _jr_all()
                    search_source = "api-catalogue-fallback"
                except Exception as api_err:
                    logger.warning(f"[SEARCH] API catalogue fallback failed: {api_err}")
        else:
            raw_results = await _mongo_search_safe(clean_keyword)
            search_source = "mongo-search"

            if not raw_results:
                logger.info("[SEARCH] MongoDB empty/unavailable — falling back to JR product-master-search API")
                raw_results = await _api_search_safe(clean_keyword)
                if raw_results:
                    search_source = "api-search-fallback"

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

        unique_results = await asyncio.to_thread(
            apply_search_pipeline,
            raw_results,
            color_check_terms=color_check_terms,
            attribute_filters=attribute_filters,
            price_filter=price_filter,
            exclude_skus=exclude_skus,
        )

        # Mongo/API raw hits can be broad — if strict filters remove everything, retry API once.
        if (
            not unique_results
            and clean_keyword
            and search_source == "mongo-search"
        ):
            logger.info("[SEARCH] 0 after filters on Mongo results — retrying JR API")
            api_results = await _api_search_safe(clean_keyword)
            if api_results:
                unique_results = await asyncio.to_thread(
                    apply_search_pipeline,
                    api_results,
                    color_check_terms=color_check_terms,
                    attribute_filters=attribute_filters,
                    price_filter=price_filter,
                    exclude_skus=exclude_skus,
                )
                if unique_results:
                    search_source = "api-search-fallback"

        if not unique_results:
            logger.warning("[SEARCH] 0 products after filters")
            return {"error": "No products found."}

        selected = random.sample(unique_results, min(3, len(unique_results)))
        logger.info(f"[SEARCH] selected {len(selected)} product(s) for response")

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
        for p in selected:
            sku = str(p.get("SKU") or p.get("BarCode") or "").strip()
            barcode = str(p.get("BarCode") or "").strip()
            price_amount = p.get(currency_field)
            display_price = build_display_price(currency, price_amount)

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
