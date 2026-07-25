"""Unit tests for Kisna-style JR search strategy list + prefer rules."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qlink_chatbot.utils.jr_search_strategies import (
    DROP_PRICE_NOTE,
    SIZE_RELAX_NOTE,
    WIDEN_PRICE_NOTE,
    build_search_strategies,
    prefer_drop_size_over_exact,
    widen_price_filter,
)


def test_strategy_order_exact_then_drops_then_price():
    filters = {
        "color_exact": {"red"},
        "shape": {"round"},
        "size_category": {"medium"},
        "room": {"living"},
    }
    price = {"operator": "$lte", "amount": 20000, "currency": "USD"}
    ids = [s.id for s in build_search_strategies("red&round&medium&living", filters, price)]
    assert ids[0] == "exact"
    assert "drop_size" in ids
    assert ids.index("drop_size") < ids.index("drop_shape")
    assert ids.index("drop_shape") < ids.index("drop_room")
    assert ids.index("drop_room") < ids.index("widen_price")
    assert ids.index("widen_price") < ids.index("drop_price")
    assert ids[-1] == "drop_price"


def test_exact_has_empty_note_and_full_filters():
    filters = {"shape": {"round"}, "size": {"8x10"}}
    strats = build_search_strategies("round&8x10", filters, None)
    exact = strats[0]
    assert exact.id == "exact"
    assert exact.note == ""
    assert exact.size_relaxed is False
    assert exact.filters["shape"] == {"round"}
    assert exact.filters["size"] == {"8x10"}


def test_drop_size_note_and_clears_size_filters():
    filters = {
        "shape": {"round"},
        "size": {"8x10"},
        "size_category": {"medium"},
    }
    strats = {s.id: s for s in build_search_strategies("round&8x10&medium", filters, None)}
    assert "drop_size" in strats
    ds = strats["drop_size"]
    assert ds.note == SIZE_RELAX_NOTE
    assert ds.size_relaxed is True
    assert not (ds.filters.get("size") or set())
    assert not (ds.filters.get("size_category") or set())
    assert ds.filters.get("shape") == {"round"}


def test_drop_shape_note_when_exact_would_be_empty():
    """Over-constrained: shape drop is present with honesty note (golden case)."""
    filters = {
        "color_exact": {"red"},
        "shape": {"round"},
        "material": {"silk"},
    }
    strats = {s.id: s for s in build_search_strategies("red&round&silk", filters, None)}
    assert strats["drop_shape"].note.startswith("No rugs matched that shape")
    assert not (strats["drop_shape"].filters.get("shape") or set())
    # Cumulative: drop_material comes after drop_shape and keeps shape cleared.
    assert "drop_material" in strats
    assert not (strats["drop_material"].filters.get("shape") or set())
    assert not (strats["drop_material"].filters.get("material") or set())


def test_widen_then_drop_price():
    price = {"operator": "$lte", "amount": 10000, "currency": "USD"}
    filters = {"shape": {"round"}}
    strats = {s.id: s for s in build_search_strategies("round", filters, price)}
    assert strats["widen_price"].note == WIDEN_PRICE_NOTE
    assert strats["widen_price"].price_filter["amount"] == 12500
    assert strats["drop_price"].note == DROP_PRICE_NOTE
    assert strats["drop_price"].price_filter is None


def test_widen_gte_lowers_floor():
    widened = widen_price_filter(
        {"operator": "$gte", "amount": 15000, "currency": "USD"},
        1.25,
    )
    assert widened["amount"] == 12000


def test_prefer_drop_size_over_weak_exact():
    assert prefer_drop_size_over_exact(
        exact_results=[],
        exact_tier=None,
        drop_size_results=[{"SKU": "A"}],
        drop_size_tier="exact_catalog_color",
    )
    assert prefer_drop_size_over_exact(
        exact_results=[{"SKU": "yarn"}],
        exact_tier="breakdown_fallback",
        drop_size_results=[{"SKU": "catalog"}],
        drop_size_tier="similar_catalog_color",
    )
    assert not prefer_drop_size_over_exact(
        exact_results=[{"SKU": "strong"}],
        exact_tier="exact_catalog_color",
        drop_size_results=[{"SKU": "nearby"}],
        drop_size_tier="exact_catalog_color",
    )
