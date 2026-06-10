"""Explain why a product was included in search results."""

from qlink_chatbot.utils.jr_search_aliases import color_search_terms
from qlink_chatbot.utils.jr_search_color_breakdown import (
    matched_breakdown_for_terms,
    user_terms_to_breakdown_colors,
)
from qlink_chatbot.utils.jr_search_mongo import (
    color_field_matches,
    product_matches_color_terms,
    product_matches_exact_grcolor_terms,
)

COLOR_TIER_METHOD = {
    "exact_color_breakdown": "product_color_breakdown",
    "similar_color_breakdown": "product_color_breakdown",
    "similar_color": "grcolor_colorfamily",
}


def compute_color_match_score(
    product: dict,
    *,
    match_terms: set[str],
    exact_color_terms: set[str],
    breakdown_by_sku: dict[str, list[dict]] | None,
) -> dict:
    """
    Rank key (highest first):
    1) matched JR.product_color breakdown % (sum, then max single color)
    2) GrColor text hit (exact user color word first)
    3) ColorFamily text hit
    4) BrColor text hit
    """
    terms = set(match_terms or set())
    for term in exact_color_terms or set():
        terms.update(color_search_terms(term))

    breakdown = matched_breakdown_for_terms(product, terms, breakdown_by_sku)
    by_color = breakdown.get("by_color") or {}
    pct_sum = round(sum(by_color.values()), 2)
    pct_max = round(max(by_color.values()), 2) if by_color else 0.0

    catalog_hits = _catalog_color_hits(product, terms) if terms else {}
    exact_grcolor = (
        product_matches_exact_grcolor_terms(product, exact_color_terms)
        if exact_color_terms
        else False
    )

    return {
        "pct_sum": pct_sum,
        "pct_max": pct_max,
        "exact_grcolor": exact_grcolor,
        "grcolor_hit": bool(catalog_hits.get("GrColor")),
        "colorfamily_hit": bool(catalog_hits.get("ColorFamily")),
        "brcolor_hit": bool(catalog_hits.get("BrColor")),
        "breakdown_matched": by_color,
        "sort_key": (
            pct_sum,
            pct_max,
            1 if exact_grcolor else int(catalog_hits.get("GrColor", False)),
            int(catalog_hits.get("ColorFamily", False)),
            int(catalog_hits.get("BrColor", False)),
        ),
    }


def rank_products_by_color_match(
    products: list[dict],
    *,
    match_terms: set[str],
    exact_color_terms: set[str],
    breakdown_by_sku: dict[str, list[dict]] | None,
    color_search_tier: str | None,
) -> list[tuple[dict, dict]]:
    """Return products sorted best color match first (stable for ties)."""
    if not products:
        return []

    has_color_signal = bool(
        color_search_tier
        or match_terms
        or exact_color_terms
    )
    if not has_color_signal:
        return [(p, {"sort_key": (0, 0, 0, 0, 0), "rank_method": "catalog_order"}) for p in products]

    scored: list[tuple[dict, dict]] = []
    for product in products:
        score = compute_color_match_score(
            product,
            match_terms=match_terms,
            exact_color_terms=exact_color_terms,
            breakdown_by_sku=breakdown_by_sku,
        )
        score["rank_method"] = "color_relevance"
        scored.append((product, score))

    scored.sort(key=lambda item: item[1]["sort_key"], reverse=True)
    return scored


def select_top_products(
    products: list[dict],
    *,
    match_terms: set[str],
    exact_color_terms: set[str],
    breakdown_by_sku: dict[str, list[dict]] | None,
    color_search_tier: str | None,
    limit: int = 3,
) -> tuple[list[dict], list[dict]]:
    """Pick top N products by color relevance instead of random sampling."""
    ranked = rank_products_by_color_match(
        products,
        match_terms=match_terms,
        exact_color_terms=exact_color_terms,
        breakdown_by_sku=breakdown_by_sku,
        color_search_tier=color_search_tier,
    )
    top = ranked[:limit]
    return [p for p, _ in top], [s for _, s in top]


def _catalog_color_hits(product: dict, terms: set[str]) -> dict[str, bool]:
    terms = {t for t in terms if t}
    grcolor = str(product.get("GrColor") or "")
    brcolor = str(product.get("BrColor") or "")
    family = str(product.get("ColorFamily") or "")
    return {
        "GrColor": any(color_field_matches(term, grcolor) for term in terms),
        "BrColor": any(color_field_matches(term, brcolor) for term in terms),
        "ColorFamily": any(color_field_matches(term, family) for term in terms),
    }


def build_product_recommendation_reason(
    product: dict,
    *,
    color_search_tier: str | None,
    match_terms: set[str],
    exact_color_terms: set[str],
    attribute_filters: dict[str, set],
    price_filter: dict | None,
    breakdown_by_sku: dict[str, list[dict]] | None,
    displayable_pool_size: int,
    rank: int | None = None,
    rank_score: dict | None = None,
) -> dict:
    sku = str(product.get("SKU") or product.get("BarCode") or "").strip()
    terms = set(match_terms or set())
    for term in exact_color_terms or set():
        terms.update(color_search_terms(term))

    breakdown = matched_breakdown_for_terms(product, terms, breakdown_by_sku)
    breakdown_palette = user_terms_to_breakdown_colors(terms)
    catalog_hits = _catalog_color_hits(product, terms) if terms else {}

    if color_search_tier in COLOR_TIER_METHOD:
        color_match_method = COLOR_TIER_METHOD[color_search_tier]
    elif terms:
        color_match_method = "catalog_keyword_match"
    else:
        color_match_method = "non_color_search"

    non_color_filters: list[str] = []
    if price_filter:
        currency = price_filter.get("currency", "INR")
        if "min_amount" in price_filter and "max_amount" in price_filter:
            non_color_filters.append(
                f"price {currency} {int(price_filter['min_amount'])}–{int(price_filter['max_amount'])}"
            )
        elif "amount" in price_filter:
            op = price_filter.get("operator", "$lte")
            word = "under" if op == "$lte" else "above"
            non_color_filters.append(f"price {word} {currency} {int(price_filter['amount'])}")

    for key, label in (
        ("shape", "shape"),
        ("size", "size"),
        ("size_cm", "size_cm"),
        ("material", "material"),
        ("construction", "construction"),
        ("pattern", "pattern"),
        ("room", "room"),
        ("multicolor", "multicolor"),
    ):
        values = attribute_filters.get(key) or set()
        if values:
            non_color_filters.append(f"{label}={sorted(values)}")

    summary_parts: list[str] = []

    if color_match_method == "product_color_breakdown":
        matched = breakdown.get("by_color") or {}
        if matched:
            pct_bits = ", ".join(
                f"{color} {pct:g}%"
                for color, pct in sorted(matched.items(), key=lambda x: -x[1])
            )
            tier_word = "exact" if color_search_tier == "exact_color_breakdown" else "similar"
            summary_parts.append(
                f"Color via JR.product_color breakdown ({tier_word}): {pct_bits}"
            )
        else:
            summary_parts.append(
                "Color via JR.product_color palette index (breakdown rows missing for SKU)"
            )
        summary_parts.append(
            f"Catalog labels — GrColor={product.get('GrColor')!r}, "
            f"BrColor={product.get('BrColor')!r}, ColorFamily={product.get('ColorFamily')!r} "
            "(informational; filter used percentage breakdown, not these labels)"
        )
    elif color_match_method == "grcolor_colorfamily" and terms:
        hits = [field for field, ok in catalog_hits.items() if ok]
        if hits:
            summary_parts.append(
                f"Color via catalog text on {', '.join(hits)} "
                f"(GrColor={product.get('GrColor')!r}, BrColor={product.get('BrColor')!r}, "
                f"ColorFamily={product.get('ColorFamily')!r})"
            )
        elif product_matches_color_terms(product, terms):
            summary_parts.append(
                "Color via GrColor/ColorFamily fallback "
                f"(GrColor={product.get('GrColor')!r}, ColorFamily={product.get('ColorFamily')!r})"
            )
        else:
            summary_parts.append("Color tier similar_color but no direct text hit on this SKU")
    elif terms:
        summary_parts.append(
            f"Matched search keyword; catalog colors GrColor={product.get('GrColor')!r}, "
            f"BrColor={product.get('BrColor')!r}"
        )
    else:
        summary_parts.append("No color filter on this search")

    if non_color_filters:
        summary_parts.append("Also matched: " + "; ".join(non_color_filters))

    if rank_score and rank_score.get("rank_method") == "color_relevance":
        pct_bits = rank_score.get("breakdown_matched") or {}
        rank_detail_parts: list[str] = []
        if pct_bits:
            rank_detail_parts.append(
                "breakdown "
                + ", ".join(
                    f"{color} {pct:g}%"
                    for color, pct in sorted(pct_bits.items(), key=lambda x: -x[1])
                )
            )
        if rank_score.get("exact_grcolor") or rank_score.get("grcolor_hit"):
            rank_detail_parts.append("GrColor match")
        if rank_score.get("colorfamily_hit"):
            rank_detail_parts.append("ColorFamily match")
        if rank_score.get("brcolor_hit"):
            rank_detail_parts.append("BrColor match")
        detail = f" ({'; '.join(rank_detail_parts)})" if rank_detail_parts else ""
        summary_parts.append(
            f"Ranked #{rank or '?'} of {displayable_pool_size} by color relevance{detail}"
        )
    else:
        summary_parts.append(
            f"Selected from {displayable_pool_size} in-stock displayable match(es) "
            "(no color ranking — non-color search)"
        )

    return {
        "SKU": sku,
        "name": (product.get("Name") or product.get("Collection") or "").strip(),
        "color_match_method": color_match_method,
        "color_search_tier": color_search_tier,
        "search_color_terms": sorted(terms),
        "breakdown_palette_colors": sorted(breakdown_palette),
        "breakdown_matched_percentages": breakdown.get("by_color") or {},
        "breakdown_highest": breakdown.get("highest") or {},
        "breakdown_full": breakdown.get("full_breakdown") or [],
        "catalog_colors": {
            "GrColor": product.get("GrColor", ""),
            "BrColor": product.get("BrColor", ""),
            "ColorFamily": product.get("ColorFamily", ""),
            "DisplayFilter": product.get("DisplayFilter", ""),
        },
        "catalog_color_text_hits": catalog_hits,
        "non_color_filters": non_color_filters,
        "displayable_pool_size": displayable_pool_size,
        "rank": rank,
        "rank_score": rank_score or {},
        "summary": " | ".join(summary_parts),
    }
