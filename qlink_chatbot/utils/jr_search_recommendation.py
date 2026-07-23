"""Explain why a product was included in search results + nearest-first color rank."""

from qlink_chatbot.utils.jr_search_aliases import color_search_terms
from qlink_chatbot.utils.jr_search_color_breakdown import (
    matched_breakdown_for_terms,
    user_terms_to_breakdown_colors,
)
from qlink_chatbot.utils.jr_search_mongo import (
    color_field_matches,
    product_matches_catalog_color,
    product_matches_color_terms,
    product_matches_exact_grcolor_terms,
)

COLOR_TIER_METHOD = {
    "exact_catalog_color": "catalog_label",
    "similar_catalog_color": "catalog_label",
    "mixture_catalog_color": "catalog_label",
    "multicolor": "catalog_label",
    "breakdown_fallback": "product_color_breakdown",
    # Legacy tiers (older logs / skip_color_post_filter paths)
    "exact_color_breakdown": "product_color_breakdown",
    "similar_color_breakdown": "product_color_breakdown",
    "similar_color": "grcolor_colorfamily",
}

# Yarn closeness bands (higher = nearer to the user's color ask).
# Applies to every palette color (purple, blue, red, …) — not purple-only.
CLOSENESS_DOMINANT = 3   # requested color leads the rug
CLOSENESS_SECONDARY = 2  # meaningful share / trusted catalog label
CLOSENESS_ACCENT = 1     # present but not what the rug is "about"
CLOSENESS_UNKNOWN = 0

DOMINANT_PCT_MIN = 50.0
SECONDARY_PCT_MIN = 25.0


def _full_yarn_by_color(
    product: dict,
    breakdown_by_sku: dict[str, list[dict]] | None,
) -> dict[str, float]:
    """All yarn colors for the SKU (not filtered to the requested palette)."""
    sku = str(product.get("SKU") or product.get("BarCode") or "").strip()
    rows = (breakdown_by_sku or {}).get(sku) if sku else None
    if not rows:
        return {}
    full: dict[str, float] = {}
    for row in rows:
        color = str(row.get("color") or "").strip()
        if not color:
            continue
        full[color] = round(full.get(color, 0.0) + float(row.get("percentage") or 0), 2)
    return full


def _requested_yarn_pct(
    full_by_color: dict[str, float],
    exact_color_terms: set[str],
) -> float:
    palette = user_terms_to_breakdown_colors(set(exact_color_terms or set()))
    if not palette or not full_by_color:
        return 0.0
    return round(sum(float(full_by_color.get(c) or 0) for c in palette), 2)


def _sole_dominant_in_full_yarn(
    full_by_color: dict[str, float],
    exact_color_terms: set[str],
) -> bool:
    """True when a requested palette color is the unique max % among ALL yarns.

    Critical: must use the full breakdown. Filtering to purple-only first made
    every Purple:15% rug look "sole dominant".
    """
    if not full_by_color:
        return False
    palette = user_terms_to_breakdown_colors(set(exact_color_terms or set()))
    if not palette:
        return False
    mx = max(full_by_color.values())
    if mx <= 0:
        return False
    max_colors = {c for c, p in full_by_color.items() if p == mx}
    return len(max_colors) == 1 and bool(max_colors & palette)


def _closeness_band(
    *,
    requested_pct: float,
    sole_dominant: bool,
    catalog_score: int,
) -> int:
    """How near is this rug to the user's color ask (all colors)."""
    if sole_dominant or requested_pct >= DOMINANT_PCT_MIN:
        return CLOSENESS_DOMINANT
    if requested_pct >= SECONDARY_PCT_MIN:
        return CLOSENESS_SECONDARY
    if requested_pct > 0:
        return CLOSENESS_ACCENT
    # No yarn rows: trust catalog labels as "near" so named colors still beat accents.
    if catalog_score >= 2:
        return CLOSENESS_DOMINANT
    if catalog_score >= 1:
        return CLOSENESS_SECONDARY
    return CLOSENESS_UNKNOWN


def _sole_dominant_requested(
    by_color: dict,
    exact_color_terms: set[str],
) -> bool:
    """Legacy helper — prefer _sole_dominant_in_full_yarn for ranking."""
    if not by_color:
        return False
    palette = user_terms_to_breakdown_colors(set(exact_color_terms or set()))
    if not palette:
        return False
    mx = max(by_color.values())
    max_colors = {c for c, p in by_color.items() if p == mx}
    return len(max_colors) == 1 and bool(max_colors & palette)


def _product_matches_size_terms(product: dict, size_terms: set[str]) -> bool:
    if not size_terms:
        return False
    from qlink_chatbot.utils.jr_search_sizes import product_matches_size_term

    return any(product_matches_size_term(product, str(term)) for term in size_terms)


def compute_color_match_score(
    product: dict,
    *,
    match_terms: set[str],
    exact_color_terms: set[str],
    breakdown_by_sku: dict[str, list[dict]] | None,
    size_terms: set[str] | None = None,
    size_relaxed: bool = False,
) -> dict:
    """
    Nearest-first rank key (highest first) — same ladder for every color:

    1) catalog GrColor exact user word > alias/DisplayFilter
    2) yarn closeness (dominant > secondary > accent > unknown)
    3) sole-dominant among ALL yarn colors
    4) requested yarn %
    5) prefer ground label over border-only
    6) exact size when size was requested / relaxed
    """
    terms = set(match_terms or set())
    for term in exact_color_terms or set():
        terms.update(color_search_terms(term))

    breakdown = matched_breakdown_for_terms(product, terms, breakdown_by_sku)
    by_color = breakdown.get("by_color") or {}
    full_by_color = _full_yarn_by_color(product, breakdown_by_sku)
    requested_pct = _requested_yarn_pct(full_by_color, exact_color_terms or set())
    if requested_pct <= 0 and by_color:
        # Fallback when exact_color_terms empty but match_terms mapped.
        requested_pct = round(sum(by_color.values()), 2)

    pct_sum = round(sum(by_color.values()), 2)
    pct_max = round(max(by_color.values()), 2) if by_color else 0.0

    catalog_hits = _catalog_color_hits(product, terms) if terms else {}
    exact_user_grcolor = (
        product_matches_exact_grcolor_terms(product, exact_color_terms)
        if exact_color_terms
        else False
    )
    catalog_primary = product_matches_catalog_color(product, terms) if terms else False
    # 2 = exact user color on GrColor, 1 = alias/DisplayFilter catalog hit, 0 = none
    catalog_score = 2 if exact_user_grcolor else (1 if catalog_primary else 0)

    sole_dom = _sole_dominant_in_full_yarn(full_by_color, exact_color_terms or set())
    closeness = _closeness_band(
        requested_pct=requested_pct,
        sole_dominant=sole_dom,
        catalog_score=catalog_score,
    )

    grcolor_hit = bool(catalog_hits.get("GrColor"))
    display_hit = bool(catalog_hits.get("DisplayFilter"))
    brcolor_hit = bool(catalog_hits.get("BrColor"))
    border_only = brcolor_hit and not grcolor_hit and not display_hit
    # Prefer ground/display labels; demote border-only matches.
    ground_prefer = 1 if (grcolor_hit or display_hit) else 0
    border_penalty = 0 if border_only else 1

    exact_size = _product_matches_size_terms(product, size_terms or set())
    size_score = 1 if exact_size else 0
    if not size_terms:
        size_score = 0

    return {
        "pct_sum": pct_sum,
        "pct_max": pct_max,
        "requested_pct": requested_pct,
        "closeness_band": closeness,
        "exact_grcolor": exact_user_grcolor,
        "catalog_primary": catalog_primary,
        "catalog_score": catalog_score,
        "sole_dominant": sole_dom,
        "grcolor_hit": grcolor_hit,
        "displayfilter_hit": display_hit,
        "colorfamily_hit": bool(catalog_hits.get("ColorFamily")),
        "brcolor_hit": brcolor_hit,
        "border_only": border_only,
        "exact_size": exact_size,
        "size_relaxed": size_relaxed,
        "breakdown_matched": by_color,
        "full_yarn": full_by_color,
        "sort_key": (
            catalog_score,
            closeness,
            1 if sole_dom else 0,
            requested_pct,
            ground_prefer,
            border_penalty,
            size_score,
        ),
    }


def rank_products_by_color_match(
    products: list[dict],
    *,
    match_terms: set[str],
    exact_color_terms: set[str],
    breakdown_by_sku: dict[str, list[dict]] | None,
    color_search_tier: str | None,
    size_terms: set[str] | None = None,
    size_relaxed: bool = False,
) -> list[tuple[dict, dict]]:
    """Return products sorted nearest color match first (stable for ties)."""
    if not products:
        return []

    has_color_signal = bool(
        color_search_tier
        or match_terms
        or exact_color_terms
    )
    if not has_color_signal:
        return [
            (p, {"sort_key": (0, 0, 0, 0, 0, 0, 0), "rank_method": "catalog_order"})
            for p in products
        ]

    scored: list[tuple[dict, dict]] = []
    for product in products:
        score = compute_color_match_score(
            product,
            match_terms=match_terms,
            exact_color_terms=exact_color_terms,
            breakdown_by_sku=breakdown_by_sku,
            size_terms=size_terms,
            size_relaxed=size_relaxed,
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
    size_terms: set[str] | None = None,
    size_relaxed: bool = False,
    limit: int = 3,
) -> tuple[list[dict], list[dict]]:
    """Pick top N products by nearest color relevance instead of random sampling."""
    ranked = rank_products_by_color_match(
        products,
        match_terms=match_terms,
        exact_color_terms=exact_color_terms,
        breakdown_by_sku=breakdown_by_sku,
        color_search_tier=color_search_tier,
        size_terms=size_terms,
        size_relaxed=size_relaxed,
    )
    top = ranked[:limit]
    return [p for p, _ in top], [s for _, s in top]


def _catalog_color_hits(product: dict, terms: set[str]) -> dict[str, bool]:
    terms = {t for t in terms if t}
    grcolor = str(product.get("GrColor") or "")
    brcolor = str(product.get("BrColor") or "")
    family = str(product.get("ColorFamily") or "")
    display = str(product.get("DisplayFilter") or "")
    return {
        "GrColor": any(color_field_matches(term, grcolor) for term in terms),
        "BrColor": any(color_field_matches(term, brcolor) for term in terms),
        "ColorFamily": any(color_field_matches(term, family) for term in terms),
        "DisplayFilter": any(color_field_matches(term, display) for term in terms),
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
    size_relaxed: bool = False,
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
        ("catalog_tag", "catalog_tag"),
        ("shape", "shape"),
        ("size", "size"),
        ("size_cm", "size_cm"),
        ("size_category", "size_category"),
        ("material", "material"),
        ("construction", "construction"),
        ("pattern", "pattern"),
        ("room", "room"),
        ("multicolor", "multicolor"),
    ):
        # When size was relaxed, don't claim exact size / size-bucket matches.
        if size_relaxed and key in {"size", "size_cm", "size_category"}:
            continue
        values = attribute_filters.get(key) or set()
        if values:
            non_color_filters.append(f"{label}={sorted(values)}")

    summary_parts: list[str] = []

    if color_match_method == "catalog_label":
        hits = [field for field, ok in catalog_hits.items() if ok]
        tier_word = color_search_tier or "catalog"
        if hits:
            summary_parts.append(
                f"Color via catalog labels ({tier_word}) on {', '.join(hits)} "
                f"(GrColor={product.get('GrColor')!r}, "
                f"DisplayFilter={product.get('DisplayFilter')!r}, "
                f"ColorFamily={product.get('ColorFamily')!r})"
            )
        else:
            summary_parts.append(
                f"Color tier {tier_word}; catalog "
                f"GrColor={product.get('GrColor')!r}, "
                f"ColorFamily={product.get('ColorFamily')!r}"
            )
        matched = breakdown.get("by_color") or {}
        if matched:
            pct_bits = ", ".join(
                f"{color} {pct:g}%"
                for color, pct in sorted(matched.items(), key=lambda x: -x[1])
            )
            summary_parts.append(f"Yarn breakdown (rank signal): {pct_bits}")
    elif color_match_method == "product_color_breakdown":
        matched = breakdown.get("by_color") or {}
        if matched:
            pct_bits = ", ".join(
                f"{color} {pct:g}%"
                for color, pct in sorted(matched.items(), key=lambda x: -x[1])
            )
            summary_parts.append(
                f"Color via JR.product_color breakdown fallback: {pct_bits}"
            )
        else:
            summary_parts.append(
                "Color via JR.product_color palette index (breakdown rows missing for SKU)"
            )
        summary_parts.append(
            f"Catalog labels — GrColor={product.get('GrColor')!r}, "
            f"BrColor={product.get('BrColor')!r}, ColorFamily={product.get('ColorFamily')!r} "
            "(weaker breakdown_fallback tier)"
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

    if size_relaxed:
        summary_parts.append(
            "Size relaxed: no exact size match for this color — showing closest color matches"
        )

    if non_color_filters:
        summary_parts.append("Also matched: " + "; ".join(non_color_filters))

    if rank_score and rank_score.get("rank_method") == "color_relevance":
        pct_bits = rank_score.get("breakdown_matched") or {}
        rank_detail_parts: list[str] = []
        if rank_score.get("catalog_score"):
            rank_detail_parts.append(f"catalog_score={rank_score.get('catalog_score')}")
        closeness = rank_score.get("closeness_band")
        if closeness is not None:
            band_name = {
                CLOSENESS_DOMINANT: "dominant",
                CLOSENESS_SECONDARY: "secondary",
                CLOSENESS_ACCENT: "accent",
                CLOSENESS_UNKNOWN: "unknown",
            }.get(closeness, str(closeness))
            rank_detail_parts.append(f"closeness={band_name}")
        if rank_score.get("sole_dominant"):
            rank_detail_parts.append("sole-dominant yarn color")
        if rank_score.get("requested_pct"):
            rank_detail_parts.append(f"requested_yarn={rank_score.get('requested_pct'):g}%")
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
        if rank_score.get("displayfilter_hit"):
            rank_detail_parts.append("DisplayFilter match")
        if rank_score.get("colorfamily_hit"):
            rank_detail_parts.append("ColorFamily match")
        if rank_score.get("border_only"):
            rank_detail_parts.append("border-only (demoted)")
        if rank_score.get("exact_size"):
            rank_detail_parts.append("exact size")
        detail = f" ({'; '.join(rank_detail_parts)})" if rank_detail_parts else ""
        summary_parts.append(
            f"Ranked #{rank or '?'} of {displayable_pool_size} by nearest color{detail}"
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
        "size_relaxed": size_relaxed,
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
