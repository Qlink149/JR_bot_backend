#!/usr/bin/env python3
"""Local 60-second catalog / search diagnostic.

Use this when the bot returns unexpected rugs and you need to tell the client
whether the gap is:

  LOGIC   - product is in Mongo (and usually API), but our filters/ranking miss it
  MONGO   - product is in JR Product Master API, but not in our Mongo (resync)
  API     - product is on the website but NOT in JR product-master API (feed gap)
  OK      - product is findable / search behaves as designed

Examples (from repo root `JR_bot_backend`):

  # 1) Why didn't bot show this website rug?
  python scripts/diagnose_catalog.py --find "laal chattan"

  # 2) Replay a user message (same path as bot search)
  python scripts/diagnose_catalog.py --query "show me red rugs above 15000 usd and below 20000 usd round shape medium size"

  # 3) Did we miss a known SKU that should match the query?
  python scripts/diagnose_catalog.py --query "red round under 20000 usd" --expect-sku PAE-5080-0001

  # 4) Skip LLM extract (faster / offline-ish) and skip live API
  python scripts/diagnose_catalog.py --query "red&round" --no-llm --no-api

Needs .env: MONGO_URI, JR_API_* (unless --no-api), OPENAI_API_KEY (unless --no-llm).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

# Allow `python scripts/diagnose_catalog.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def _summarize_product(p: dict, *, source: str = "") -> dict[str, Any]:
    raw = p.get("raw") if isinstance(p.get("raw"), dict) else p
    return {
        "source": source or None,
        "SKU": raw.get("SKU") or p.get("SKU"),
        "BarCode": raw.get("BarCode") or p.get("BarCode"),
        "Name": raw.get("Name"),
        "Collection": raw.get("Collection"),
        "Shape": raw.get("Shape"),
        "SizeInFT": raw.get("SizeInFT"),
        "SizeGroupInFT": raw.get("SizeGroupInFT"),
        "GrColor": raw.get("GrColor"),
        "ColorFamily": raw.get("ColorFamily"),
        "DisplayFilter": raw.get("DisplayFilter"),
        "INR_MRP": raw.get("INR_MRP"),
        "USD_MRP": raw.get("USD_MRP"),
        "LiveStatus": raw.get("LiveStatus"),
        "Published": raw.get("Published"),
        "Material": raw.get("Material"),
        "Construction": raw.get("Construction"),
        "ProductURL": raw.get("ProductURL"),
        "inStock_flag": (p.get("flags") or {}).get("inStock") if "flags" in p else None,
        "has_red_token": "red" in (p.get("search_tokens") or []),
        "has_round_token": "round" in (p.get("search_tokens") or []),
    }


def _needle_regex(needle: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in re.split(r"\s+", needle.strip()) if p]
    if not parts:
        return re.compile(r"$^")
    return re.compile("|".join(parts), re.IGNORECASE)


def _blob_match(obj: dict, needle: str) -> bool:
    rx = _needle_regex(needle)
    blob = " ".join(
        str(obj.get(k) or "")
        for k in (
            "SKU",
            "BarCode",
            "Name",
            "Collection",
            "ProductURL",
            "DesignName",
            "GrColor",
            "ColorFamily",
        )
    )
    return bool(rx.search(blob))


def find_in_mongo(needle: str, *, limit: int = 10) -> list[dict]:
    from qlink_chatbot.utils.jr_search_mongo import products_collection

    rx = re.compile(re.escape(needle.strip()), re.IGNORECASE)
    query = {
        "$or": [
            {"SKU": {"$regex": rx}},
            {"BarCode": {"$regex": rx}},
            {"raw.SKU": {"$regex": rx}},
            {"raw.BarCode": {"$regex": rx}},
            {"raw.Name": {"$regex": rx}},
            {"raw.Collection": {"$regex": rx}},
            {"raw.ProductURL": {"$regex": rx}},
            {"search_tokens": needle.strip().lower()},
        ]
    }
    docs = list(
        products_collection.find(query).limit(limit)
    )
    # Also token-ish multiword: match all words in Name/Collection/URL
    if not docs and " " in needle.strip():
        words = [w.lower() for w in needle.split() if len(w) > 2]
        and_clauses = []
        for w in words:
            and_clauses.append(
                {
                    "$or": [
                        {"raw.Name": {"$regex": w, "$options": "i"}},
                        {"raw.Collection": {"$regex": w, "$options": "i"}},
                        {"raw.ProductURL": {"$regex": w, "$options": "i"}},
                        {"search_tokens": w},
                    ]
                }
            )
        if and_clauses:
            docs = list(products_collection.find({"$and": and_clauses}).limit(limit))
    return docs


async def find_in_api_search(needle: str) -> list[dict]:
    from qlink_chatbot.utils.jr_api_client import search_products

    rows = await search_products(needle)
    return [p for p in rows if _blob_match(p, needle)] or list(rows)[:5]


async def find_in_api_master(needle: str, *, limit: int = 10) -> tuple[int, list[dict]]:
    from qlink_chatbot.utils.jr_api_client import get_all_products

    products = await get_all_products()
    hits = [p for p in products if _blob_match(p, needle)]
    return len(products), hits[:limit]


async def run_bot_search(
    query: str,
    *,
    use_llm: bool = True,
    currency: str = "USD",
    country: str = "IN",
) -> dict[str, Any]:
    from qlink_chatbot.utils.jaipur_rugs_api import jaipur_rugs_product_search
    from qlink_chatbot.utils.jr_search_keywords import normalise_keyword
    from qlink_chatbot.utils.jr_search_llm_extract import resolve_keyword_with_llm_extraction
    from qlink_chatbot.utils.jr_search_mongo import apply_search_pipeline, mongo_search_products

    debug: list = []
    keyword = query
    llm_debug = None
    if use_llm:
        keyword, llm_debug = await resolve_keyword_with_llm_extraction(
            query,
            user_message=query,
            detected_currency=currency,
            country_code=country,
        )
        if llm_debug:
            debug.append(llm_debug)

    if llm_debug and llm_debug.get("use_llm_payload") and llm_debug.get("search_payload"):
        payload = llm_debug["search_payload"]
        price_filter = payload.get("price_filter")
        clean_keyword = (payload.get("clean_keyword") or "").strip()
        color_check_terms = payload.get("color_check_terms") or set()
        attribute_filters = payload.get("attribute_filters") or {}
        norm_source = "llm_primary_payload"
    else:
        price_filter, clean_keyword, color_check_terms, attribute_filters = normalise_keyword(
            keyword or query
        )
        norm_source = "regex_normalise_keyword"

    raw, color_pre = mongo_search_products(clean_keyword)
    filtered, tier, meta = apply_search_pipeline(
        raw,
        color_check_terms=color_check_terms,
        attribute_filters=attribute_filters,
        price_filter=price_filter,
        exclude_skus=None,
        skip_color_post_filter=color_pre,
    )

    # Full bot path (includes progressive relax)
    pool: list = []
    page = await jaipur_rugs_product_search(
        keyword or query,
        country_code=country,
        requested_currency=currency,
        user_message=query,
        skip_llm_extraction=not use_llm,
        extraction_debug_out=debug if use_llm else None,
        pool_out=pool,
        page_size=3,
        pool_size=12,
    )

    attrs = {}
    if debug and isinstance(debug[0], dict):
        attrs = debug[0].get("attributes") or {}

    return {
        "resolved_keyword": keyword,
        "norm_source": norm_source,
        "clean_keyword": clean_keyword,
        "price_filter": price_filter,
        "attribute_filters": {
            k: sorted(v) if isinstance(v, set) else v
            for k, v in (attribute_filters or {}).items()
            if v
        },
        "llm_attributes": attrs,
        "mongo_raw_count": len(raw),
        "pipeline_strict_count": len(filtered),
        "pipeline_tier": tier,
        "pipeline_meta": meta,
        "pipeline_top": [_summarize_product(p, source="pipeline_strict") for p in filtered[:5]],
        "bot_page": page if isinstance(page, list) else page,
        "bot_pool_count": len(pool),
        "bot_top": [
            {
                "SKU": p.get("SKU"),
                "name": p.get("name"),
                "shape": p.get("shape"),
                "size": p.get("size"),
                "display_price": p.get("display_price"),
                "color": p.get("color"),
                "color_family": p.get("color_family"),
                "size_relaxed": p.get("size_relaxed"),
                "fallback_note": p.get("fallback_note"),
            }
            for p in (page if isinstance(page, list) else [])[:5]
        ],
        "fallback_note": next(
            (p.get("fallback_note") for p in (page if isinstance(page, list) else []) if p.get("fallback_note")),
            "",
        ),
    }


def verdict_for_find(
    *,
    needle: str,
    mongo_hits: list,
    api_search_hits: list | None,
    api_master_hits: list | None,
) -> str:
    in_mongo = bool(mongo_hits)
    in_api_search = None if api_search_hits is None else bool(api_search_hits)
    in_api_master = None if api_master_hits is None else bool(api_master_hits)

    if in_mongo:
        return (
            "VERDICT: IN_MONGO - product is synced. If bot didn't show it, this is a "
            "LOGIC / filter / ranking issue (not a missing catalog row)."
        )
    if in_api_master:
        return (
            "VERDICT: API_YES_MONGO_NO - Product Master has it, Mongo doesn't. "
            "Run product sync (POST /api/sync-products or cron)."
        )
    if in_api_search and not in_mongo:
        return (
            "VERDICT: API_SEARCH_HIT_MONGO_MISS - search API returned needle matches, "
            "but Mongo has no row. Check SKU/Barcode sync + LiveStatus/Published."
        )
    if in_api_master is False and not in_mongo:
        return (
            "VERDICT: API_GAP - not in JR product-master feed (and not in Mongo). "
            "Website can still show it from another CMS. Escalate to JR API team."
        )
    if in_api_search is False and not in_mongo:
        return (
            f"VERDICT: LIKELY_API_GAP - {needle!r} not in Mongo; "
            "product-master-search empty for needle. "
            "Confirm with --full-api for hard proof from full master dump."
        )
    return "VERDICT: UNKNOWN - rerun with API enabled (--full-api for hard proof)."


def verdict_for_expect(search: dict, expect_sku: str) -> str:
    sku = expect_sku.strip().upper()
    page = search.get("bot_page") if isinstance(search.get("bot_page"), list) else []
    pool_skus = {str(p.get("SKU") or "").upper() for p in page}
    # also check strict pipeline
    pipe_skus = {str(p.get("SKU") or "").upper() for p in search.get("pipeline_top") or []}
    from qlink_chatbot.utils.jr_search_mongo import products_collection

    doc = products_collection.find_one({"SKU": {"$regex": f"^{re.escape(expect_sku)}$", "$options": "i"}})
    if not doc:
        doc = products_collection.find_one({"raw.SKU": {"$regex": f"^{re.escape(expect_sku)}$", "$options": "i"}})
    if not doc:
        return (
            f"VERDICT: MONGO_MISSING expected SKU {sku} - not in our DB. "
            "Check API with --find that SKU."
        )
    raw = doc.get("raw") or {}
    summary = _summarize_product(doc, source="mongo")
    if sku in pool_skus or any(sku in s for s in pool_skus):
        return f"VERDICT: OK - expected SKU {sku} is in bot page results."
    if sku in pipe_skus:
        return (
            f"VERDICT: LOGIC_PARTIAL - SKU {sku} survives strict pipeline but not final page "
            f"(ranking/pool). Product colors={summary.get('GrColor')!r} "
            f"family={summary.get('ColorFamily')!r} usd={summary.get('USD_MRP')}"
        )
    note = search.get("fallback_note") or ""
    return (
        f"VERDICT: LOGIC - SKU {sku} is IN Mongo (instock={summary.get('inStock_flag')}, "
        f"shape={summary.get('Shape')}, size={summary.get('SizeInFT')}, "
        f"GrColor={summary.get('GrColor')!r}, ColorFamily={summary.get('ColorFamily')!r}, "
        f"USD={summary.get('USD_MRP')}, red_token={summary.get('has_red_token')}) "
        f"but bot did not return it. "
        f"clean_keyword={search.get('clean_keyword')!r} price={search.get('price_filter')} "
        f"fallback_note={note!r}. "
        "Typical cause: strict color AND (family red not counted) or size/shape hard filter."
    )


def _print(title: str, payload: Any) -> None:
    print(f"\n=== {title} ===")
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    else:
        print(payload)


async def async_main(args: argparse.Namespace) -> int:
    if not args.find and not args.query:
        print("Provide --find NEEDLE and/or --query USER_MESSAGE", file=sys.stderr)
        return 2

    if args.find:
        needle = args.find.strip()
        mongo_hits = find_in_mongo(needle)
        _print(
            f"MONGO find {needle!r}",
            [_summarize_product(d, source="mongo") for d in mongo_hits],
        )

        api_search_hits = None
        api_master_hits = None
        if not args.no_api:
            try:
                api_search_hits = await find_in_api_search(needle)
                # If search returns unrelated top rows, keep only blob matches
                matched = [p for p in api_search_hits if _blob_match(p, needle)]
                _print(
                    f"API product-master-search {needle!r}",
                    {
                        "raw_count": len(api_search_hits),
                        "needle_matches": len(matched),
                        "rows": [_summarize_product(p, source="api_search") for p in (matched or api_search_hits)[:8]],
                    },
                )
                api_search_hits = matched
            except Exception as err:
                _print("API search ERROR", str(err))
                api_search_hits = []

            if args.full_api:
                try:
                    total, api_master_hits = await find_in_api_master(needle)
                    _print(
                        f"API full product-master ({total} skus)",
                        [_summarize_product(p, source="api_master") for p in api_master_hits],
                    )
                except Exception as err:
                    _print("API full master ERROR", str(err))
                    api_master_hits = []

        print("\n" + verdict_for_find(
            needle=needle,
            mongo_hits=mongo_hits,
            api_search_hits=api_search_hits,
            api_master_hits=api_master_hits,
        ))

    if args.query:
        search = await run_bot_search(
            args.query,
            use_llm=not args.no_llm,
            currency=args.currency,
            country=args.country,
        )
        _print("BOT SEARCH REPLAY", {
            k: search[k]
            for k in (
                "resolved_keyword",
                "norm_source",
                "clean_keyword",
                "price_filter",
                "attribute_filters",
                "llm_attributes",
                "mongo_raw_count",
                "pipeline_strict_count",
                "pipeline_tier",
                "pipeline_meta",
                "fallback_note",
                "bot_pool_count",
                "bot_top",
                "pipeline_top",
            )
        })
        if args.expect_sku:
            print("\n" + verdict_for_expect(search, args.expect_sku))
        elif isinstance(search.get("bot_page"), dict) and search["bot_page"].get("error"):
            print("\nVERDICT: EMPTY - bot returned no products:", search["bot_page"])
        elif search.get("fallback_note"):
            print(
                "\nVERDICT: LOGIC_RELAXED - bot returned products only after dropping filters. "
                f"Note: {search['fallback_note']!r}. "
                "If the website shows a round/red hit in-band, check --find that product "
                "(API gap vs ColorFamily strictness)."
            )
        else:
            print("\nVERDICT: OK_OR_CHECK - bot returned a page; spot-check SKUs vs website.")

    print(
        "\nClient one-liner tips:\n"
        "  API_GAP  = website-only item; JR must add to Product Master\n"
        "  MONGO    = run sync; API has it, DB doesn't\n"
        "  LOGIC    = item in DB; fix filters/ranking (often ColorFamily / medium)\n"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Diagnose JR bot vs Mongo vs Product Master API gaps",
    )
    p.add_argument("--find", help="Product needle: name / SKU / barcode / url slug")
    p.add_argument("--query", help="User message to replay through bot search")
    p.add_argument("--expect-sku", help="SKU that should appear for --query")
    p.add_argument("--currency", default="USD")
    p.add_argument("--country", default="IN")
    p.add_argument("--no-llm", action="store_true", help="Skip LLM extract (use regex only)")
    p.add_argument("--no-api", action="store_true", help="Skip JR API calls")
    p.add_argument(
        "--full-api",
        action="store_true",
        help="Also scan full product-master dump (~15k rows, ~20-40s)",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
