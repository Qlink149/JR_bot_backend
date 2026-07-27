"""Kisna-style search strategy list for JR progressive relax.

Each strategy is an independent attempt: (filters, keyword, price, note, id).
Orchestration tries them in order; first non-empty pool wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qlink_chatbot.utils.jr_search_mongo import (
    classify_segment,
    filters_without_size,
    strip_size_segments_from_keyword,
)


@dataclass
class SearchStrategy:
    id: str
    keyword: str
    filters: dict[str, Any]
    price_filter: dict | None
    note: str = ""
    size_relaxed: bool = False


_DROP_ATTR_STEPS: tuple[tuple[str, str, str], ...] = (
    (
        "shape",
        "drop_shape",
        "No rugs matched that shape with your other filters — showing other shapes.",
    ),
    ("room", "drop_room", "Broadened search by dropping the room filter."),
    ("weight_max", "drop_weight", "Broadened search by dropping the weight filter."),
    # catalog_tag is NEVER dropped here — showing untagged rugs as "new/bestsellers"
    # is worse than an honest empty. Tag+price can still widen/drop price with tag kept.
    ("pattern", "drop_pattern", "Broadened search by dropping the pattern filter."),
    (
        "construction",
        "drop_construction",
        "Broadened search by dropping the construction filter.",
    ),
    ("material", "drop_material", "Broadened search by dropping the material filter."),
)

SIZE_RELAX_NOTE = (
    "No exact size match — showing closest available sizes that fit your other filters."
)
WIDEN_PRICE_NOTE = "No exact budget match — showing a slightly wider price range."
DROP_PRICE_NOTE = (
    "No rugs in that exact price range — showing closest matches with your other filters."
)


def copy_attribute_filters(attribute_filters: dict | None) -> dict:
    out: dict[str, Any] = {}
    for key, values in (attribute_filters or {}).items():
        out[key] = set(values) if isinstance(values, (set, list, tuple)) else values
    return out


def filters_without_keys(attribute_filters: dict | None, *keys: str) -> dict:
    out = copy_attribute_filters(attribute_filters)
    for key in keys:
        if key in out and isinstance(out[key], set):
            out[key] = set()
        elif key in out:
            out[key] = None
    return out


def strip_values_from_keyword(keyword: str, values) -> str:
    drop = {str(v).strip().lower() for v in (values or []) if v}
    if not drop:
        return keyword or ""
    kept = [
        part.strip()
        for part in (keyword or "").split("&")
        if part.strip() and part.strip().lower() not in drop
    ]
    return "&".join(kept)


def strip_segment_types_from_keyword(keyword: str, segment_types: set[str]) -> str:
    if not keyword or not segment_types:
        return keyword or ""
    kept = [
        part.strip()
        for part in keyword.split("&")
        if part.strip() and classify_segment(part.strip()) not in segment_types
    ]
    return "&".join(kept)


def apply_drop_to_keyword(keyword: str, drop_key: str, values) -> str:
    """Strip keyword segments for a dropped attribute key."""
    if drop_key == "weight_max":
        return strip_segment_types_from_keyword(keyword, {"weight"})
    if drop_key == "catalog_tag":
        kw = strip_values_from_keyword(keyword, values)
        return strip_segment_types_from_keyword(kw, {"catalog_tag"})
    if drop_key in {"size", "size_cm", "size_category"}:
        kw = strip_values_from_keyword(keyword, values)
        return strip_segment_types_from_keyword(kw, {"size"}) or kw
    if drop_key in {"shape", "room", "pattern", "construction", "material"}:
        kw = strip_values_from_keyword(keyword, values)
        return strip_segment_types_from_keyword(kw, {drop_key}) or kw
    return strip_values_from_keyword(keyword, values)


def widen_price_filter(price_filter: dict | None, factor: float = 1.25) -> dict | None:
    """Widen a budget band. For $gte floors, lower the threshold (amount / factor)."""
    if not price_filter:
        return None
    out = dict(price_filter)
    op = (out.get("operator") or "").strip()
    if "amount" in out and out["amount"] is not None:
        amount = float(out["amount"])
        if op == "$gte":
            out["amount"] = int(amount / factor)
        else:
            out["amount"] = int(amount * factor)
    if "max_amount" in out and out["max_amount"] is not None:
        out["max_amount"] = int(float(out["max_amount"]) * factor)
    if "min_amount" in out and out["min_amount"] is not None:
        out["min_amount"] = int(float(out["min_amount"]) / factor)
    return out


def _has_filter_values(filters: dict, key: str) -> bool:
    values = (filters or {}).get(key)
    if isinstance(values, set):
        return bool(values)
    return bool(values)


def _has_size_filters(filters: dict) -> bool:
    return any(
        _has_filter_values(filters, k) for k in ("size", "size_cm", "size_category")
    )


def _strategy_key(strat: SearchStrategy) -> tuple:
    """Dedupe strategies that are identical in effect."""
    filt = strat.filters or {}
    filt_sig = tuple(
        sorted(
            (k, tuple(sorted(str(x) for x in (v or set()))))
            for k, v in filt.items()
            if isinstance(v, set)
        )
    )
    price = strat.price_filter or {}
    price_sig = tuple(sorted((k, price[k]) for k in sorted(price.keys())))
    return (strat.keyword or "", filt_sig, price_sig)


def build_search_strategies(
    clean_keyword: str,
    attribute_filters: dict | None,
    price_filter: dict | None,
) -> list[SearchStrategy]:
    """Ordered attempts: exact → drop size → drop attrs → widen price → drop price."""
    base_filters = copy_attribute_filters(attribute_filters)
    base_keyword = (clean_keyword or "").strip()
    strategies: list[SearchStrategy] = []
    seen: set[tuple] = set()

    def add(strat: SearchStrategy) -> None:
        key = _strategy_key(strat)
        if key in seen:
            return
        seen.add(key)
        strategies.append(strat)

    add(
        SearchStrategy(
            id="exact",
            keyword=base_keyword,
            filters=copy_attribute_filters(base_filters),
            price_filter=dict(price_filter) if price_filter else None,
            note="",
            size_relaxed=False,
        )
    )

    if _has_size_filters(base_filters):
        size_kw = strip_size_segments_from_keyword(base_keyword) or base_keyword
        size_values: set = set()
        for k in ("size", "size_cm", "size_category"):
            size_values |= set(base_filters.get(k) or set())
        size_kw = apply_drop_to_keyword(size_kw, "size_category", size_values) or size_kw
        add(
            SearchStrategy(
                id="drop_size",
                keyword=size_kw,
                filters=filters_without_size(base_filters),
                price_filter=dict(price_filter) if price_filter else None,
                note=SIZE_RELAX_NOTE,
                size_relaxed=True,
            )
        )

    # Cumulative attr drops (same as prior progressive loop): each step drops one
    # more facet from the previous working filters, starting from exact/base.
    working_filters = copy_attribute_filters(base_filters)
    working_keyword = base_keyword
    for drop_key, strat_id, note in _DROP_ATTR_STEPS:
        if not _has_filter_values(working_filters, drop_key):
            continue
        values = working_filters.get(drop_key) or set()
        working_filters = filters_without_keys(working_filters, drop_key)
        working_keyword = apply_drop_to_keyword(working_keyword, drop_key, values)
        add(
            SearchStrategy(
                id=strat_id,
                keyword=working_keyword,
                filters=copy_attribute_filters(working_filters),
                price_filter=dict(price_filter) if price_filter else None,
                note=note,
                size_relaxed=False,
            )
        )

    # Price widen/drop use the most-relaxed attr filters so far (matches old
    # progressive: after attr steps fail, widen/drop against working filters).
    price_filters = copy_attribute_filters(working_filters)
    price_keyword = working_keyword or base_keyword
    if price_filter:
        widened = widen_price_filter(price_filter, 1.25)
        if widened and widened != price_filter:
            add(
                SearchStrategy(
                    id="widen_price",
                    keyword=price_keyword,
                    filters=price_filters,
                    price_filter=widened,
                    note=WIDEN_PRICE_NOTE,
                    size_relaxed=False,
                )
            )
        add(
            SearchStrategy(
                id="drop_price",
                keyword=price_keyword,
                filters=price_filters,
                price_filter=None,
                note=DROP_PRICE_NOTE,
                size_relaxed=False,
            )
        )

    return strategies


# Mongo empty-$and bootstrap (recall) — kept separate from post-filter strategies.
# Drop room before shape so "runner&hallway" recovers runners (hallway is sparse).
# Never drop catalog_tag here — empty tagged pool must stay empty, not random rugs.
MONGO_SEGMENT_DROP_ORDER: tuple[tuple[str, str], ...] = (
    ("size", "No exact size match in catalog — showing other sizes."),
    ("room", "Broadened search by dropping the room filter."),
    ("shape", "No rugs matched that shape with your other filters — showing other shapes."),
    ("pattern", "Broadened search by dropping the pattern filter."),
    ("construction", "Broadened search by dropping the construction filter."),
    ("material", "Broadened search by dropping the material filter."),
    ("weight", "Broadened search by dropping the weight filter."),
    ("general", "Broadened search — showing closer catalog matches."),
)

SEGMENT_TYPE_TO_FILTER_KEYS: dict[str, tuple[str, ...]] = {
    "size": ("size", "size_cm", "size_category"),
    "shape": ("shape",),
    "room": ("room",),
    "pattern": ("pattern",),
    "construction": ("construction",),
    "material": ("material",),
    "catalog_tag": ("catalog_tag",),
    "weight": ("weight_max",),
}

_CATALOG_COLOR_TIERS = frozenset({
    "exact_catalog_color",
    "similar_catalog_color",
    "mixture_catalog_color",
})


def prefer_drop_size_over_exact(
    *,
    exact_results: list,
    exact_tier: str | None,
    drop_size_results: list,
    drop_size_tier: str | None,
) -> bool:
    """True when size-relaxed catalog color should beat weak/empty exact."""
    if not drop_size_results:
        return False
    if not exact_results:
        return True
    if exact_tier in {None, "breakdown_fallback"} and drop_size_tier in _CATALOG_COLOR_TIERS:
        return True
    # Prefer true ground-color with nearby size over ColorFamily-only with exact size.
    if exact_tier == "similar_catalog_color" and drop_size_tier == "exact_catalog_color":
        return True
    return False
