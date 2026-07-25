"""Regression tests for live stress-test gaps (show-more pin, shape, yes, handoff)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qlink_chatbot.agent.chat_agent import (
    _extract_show_more_keyword,
    _is_agent_handoff_query,
    _is_custom_rug_query,
    _is_new_product_search_request,
    _merge_search_with_previous,
    _prior_user_search_message,
)
from qlink_chatbot.utils.jr_search_keywords import normalise_keyword
from qlink_chatbot.utils.jr_search_llm_extract import resolve_hygienic_search_keyword
from qlink_chatbot.utils.jr_search_mongo import product_matches_shape, shape_field_matches


def test_shape_matches_size_prefixed_round():
    assert shape_field_matches("round", "9 Round")
    assert shape_field_matches("round", "9 round")
    assert product_matches_shape({"Shape": "9 Round", "SizeInFT": "9 round"}, "round")
    assert product_matches_shape({"Shape": "", "SizeInFT": "8 Round"}, "round")


def test_shape_matches_rectangle_from_size_text():
    assert product_matches_shape(
        {"Shape": "", "SizeInFT": "2'6X4"},
        "rectangle",
    )
    assert not product_matches_shape(
        {"Shape": "", "SizeInFT": "8 Round"},
        "rectangle",
    )


def test_devanagari_laal_gol_extracts_red_and_round():
    _pf, clean, colors, attrs = normalise_keyword("मुझे लाल गोल दर्री दिखाओ")
    assert "red" in (colors or set()) or "red" in (clean or "").lower()
    shapes = attrs.get("shape") or set()
    assert any("round" in str(s).lower() for s in shapes) or "gol" in (clean or "").lower() or "round" in (clean or "").lower()


def test_handoff_and_custom_not_product_search():
    assert _is_agent_handoff_query("I want to talk to a human agent")
    assert _is_custom_rug_query("I want a custom rug with my own design")
    assert not _is_new_product_search_request(
        "I want to talk to a human agent", [], []
    )
    assert not _is_new_product_search_request(
        "I want a custom rug with my own design", [], []
    )


def test_hygienic_rejects_yes_keyword():
    assert resolve_hygienic_search_keyword({"keyword": "yes"}, "yes") == ""
    assert resolve_hygienic_search_keyword({"keyword": "yes"}, "bhura medium") == "bhura medium"


def test_prior_user_search_skips_affirmative():
    history = [
        {"role": "user", "content": "bhura medium size rugs"},
        {"role": "assistant", "content": "Want me to search brown medium rugs?"},
        {"role": "user", "content": "yes"},
    ]
    assert _prior_user_search_message(history) == "bhura medium size rugs"


def test_merge_only_wool_keeps_prior_price():
    previous = [{"keyword": "under USD 10000", "results": [{"SKU": "X"}]}]
    merged = _merge_search_with_previous("wool", "only wool please", previous)
    assert "wool" in merged.lower()
    assert "10000" in merged or "under" in merged.lower()


def test_merge_only_round_keeps_prior_color():
    previous = [{"keyword": "red", "results": [{"SKU": "X"}]}]
    merged = _merge_search_with_previous("round", "only round", previous)
    assert "red" in merged.lower()
    assert "round" in merged.lower()


def test_extract_show_more_size():
    assert _extract_show_more_keyword("show more 5x8") == "5x8"
    assert _extract_show_more_keyword("show more") == ""


def test_never_drop_catalog_tag_strategy():
    from qlink_chatbot.utils.jr_search_strategies import build_search_strategies

    ids = [
        s.id
        for s in build_search_strategies(
            "new",
            {"catalog_tag": {"new"}},
            None,
        )
    ]
    assert "exact" in ids
    assert "drop_catalog_tag" not in ids


def test_hallway_injects_runner_shape():
    _pf, clean, _colors, attrs = normalise_keyword("runner rugs for hallway under USD 2000")
    assert "hallway" in (attrs.get("room") or set())
    assert "runner" in {str(s).lower() for s in (attrs.get("shape") or set())}
    assert "runner" in clean.lower()


def test_hallway_room_matches_runner_product():
    from qlink_chatbot.utils.jr_search_mongo import product_matches_room

    runner = {"Room": "", "Shape": "Runner", "SizeInFT": "2'6x8", "MultiFilter": ""}
    living = {"Room": "Living Room", "Shape": "Rectangle", "SizeInFT": "8x10"}
    assert product_matches_room(runner, "hallway")
    assert product_matches_room(living, "living room")
    assert not product_matches_room(living, "hallway")


def test_gte_price_proximity_prefers_above_floor():
    from qlink_chatbot.utils.jr_search_recommendation import select_top_products

    products = [
        {"SKU": "LOW", "USD_MRP": "16302"},
        {"SKU": "HIGH", "USD_MRP": "50085"},
        {"SKU": "MID", "USD_MRP": "17856"},
    ]
    original = {"currency": "USD", "amount": 20000, "operator": "$gte"}
    widened = {"currency": "USD", "amount": 16000, "operator": "$gte"}
    top, _ = select_top_products(
        products,
        match_terms=set(),
        exact_color_terms=set(),
        breakdown_by_sku={},
        color_search_tier=None,
        limit=3,
        price_filter=widened,
        original_price_filter=original,
    )
    assert top[0]["SKU"] == "HIGH"
    assert {p["SKU"] for p in top[:1]} == {"HIGH"}
