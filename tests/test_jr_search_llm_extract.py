"""Unit tests for LLM structured extraction helpers (no OpenAI required)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qlink_chatbot.utils.jr_search_currency import (
    extract_price_filter_from_text,
    is_price_filter_suspicious,
)
from qlink_chatbot.utils.jr_search_keywords import normalise_keyword, resolve_search_keyword
from qlink_chatbot.utils.jr_search_llm_extract import (
    attributes_to_catalog_keyword,
    attributes_to_keyword_string,
    build_search_payload_from_attrs,
    is_weak_extraction,
    merge_extraction_keywords,
    reconcile_price_with_source,
    serialise_for_json,
    should_run_llm_extraction,
    validate_extracted_attributes,
)


def _v2_raw(**overrides) -> dict:
    base = {
        "colors": [],
        "shapes": [],
        "sizes_ft": [],
        "sizes_cm": [],
        "size_categories": [],
        "materials": [],
        "constructions": [],
        "patterns": [],
        "rooms": [],
        "multicolor": False,
        "weight_max_kg": None,
        "has_price_filter": False,
        "price_currency": None,
        "price_type": "none",
        "price_amount": None,
        "price_min": None,
        "price_max": None,
        "price_raw_phrase": None,
        "sku": None,
        "collection": None,
        "refinement": "new",
    }
    base.update(overrides)
    return base


def test_is_weak_extraction_red_is_strong():
    assert is_weak_extraction("red", extraction_mode="hybrid") is False


def test_is_weak_extraction_structured_keyword_is_strong():
    assert is_weak_extraction("blue&round", extraction_mode="hybrid") is False


def test_is_weak_extraction_price_is_strong():
    assert is_weak_extraction("under INR 50000", extraction_mode="hybrid") is False


def test_is_weak_extraction_vague_is_weak():
    assert is_weak_extraction("show me something nice", extraction_mode="hybrid") is True


def test_is_weak_extraction_disabled_in_regex_mode():
    assert is_weak_extraction("show me something nice", extraction_mode="regex") is False


def test_price_50k_usd_parses_correctly():
    pf, _ = extract_price_filter_from_text("above 50k usd")
    assert pf is not None
    assert pf["amount"] == 50_000
    assert pf["currency"] == "USD"
    assert pf["operator"] == "$gte"


def test_suspicious_price_50k_before_fix_pattern():
    pf, _ = extract_price_filter_from_text("above 50k usd")
    assert is_price_filter_suspicious("above 50k usd", pf) is False


def test_should_run_llm_hybrid_skips_strong_red():
    run, reason = should_run_llm_extraction("red", extraction_mode="hybrid")
    assert run is False
    assert reason == "strong_regex"


def test_should_run_llm_primary_always():
    run, reason = should_run_llm_extraction(
        "red",
        extraction_mode="llm_primary",
    )
    assert run is True
    assert reason == "mode_llm_primary"


def test_merge_prefer_llm_price():
    base = "blue&under INR 30000"
    llm = "blue&round&above USD 50000"
    merged = merge_extraction_keywords(base, llm, prefer_llm_price=True)
    assert "above USD 50000" in merged
    assert "under INR 30000" not in merged
    assert "round" in merged


def test_validate_drops_unknown_color():
    attrs, dropped = validate_extracted_attributes(_v2_raw(colors=["turquoise"]))
    assert attrs["colors"] == []
    assert any("color:turquoise" in d for d in dropped)


def test_validate_accepts_catalog_color():
    attrs, dropped = validate_extracted_attributes(_v2_raw(
        colors=["blue"],
        shapes=["round"],
        sizes_ft=["8x10"],
        materials=["wool"],
        patterns=["modern"],
        rooms=["living room"],
        weight_max_kg=8,
        has_price_filter=True,
        price_currency="INR",
        price_type="lte",
        price_amount=50000,
        price_raw_phrase="under INR 50000",
    ))
    assert attrs["colors"] == ["blue"]
    assert attrs["shapes"] == ["round"]
    assert attrs["sizes_ft"] == ["8x10"]
    assert attrs["materials"] == ["wool"]
    assert attrs["patterns"] == ["modern"]
    assert attrs["rooms"] == ["living room"]
    assert attrs["weight_max_kg"] == 8
    assert attrs["price"]["operator"] == "lte"
    assert attrs["price"]["amount"] == 50000
    assert dropped == []


def test_validate_50k_usd_gte():
    attrs, dropped = validate_extracted_attributes(
        _v2_raw(
            has_price_filter=True,
            price_currency="USD",
            price_type="gte",
            price_amount=50000,
            price_raw_phrase="50k usd",
        ),
        source_text="show me above 50k usd",
        default_currency="USD",
    )
    assert attrs["price"]["currency"] == "USD"
    assert attrs["price"]["operator"] == "gte"
    assert attrs["price"]["amount"] == 50000


def test_reconcile_corrects_hallucinated_50_to_50000():
    llm_price = {"currency": "USD", "operator": "gte", "amount": 50}
    fixed, notes = reconcile_price_with_source(
        llm_price,
        "show me above 50k usd",
        default_currency="USD",
    )
    assert fixed["amount"] == 50000
    assert any("corrected" in n for n in notes)


def test_build_search_payload_price_only():
    attrs, _ = validate_extracted_attributes(
        _v2_raw(
            has_price_filter=True,
            price_currency="USD",
            price_type="gte",
            price_amount=50000,
            price_raw_phrase="50k usd",
        ),
        source_text="above 50k usd",
    )
    payload = build_search_payload_from_attrs(attrs)
    assert payload["clean_keyword"] == ""
    assert payload["price_filter"]["currency"] == "USD"
    assert payload["price_filter"]["amount"] == 50000
    assert payload["price_filter"]["operator"] == "$gte"


def test_attributes_to_keyword_string():
    attrs, _ = validate_extracted_attributes(_v2_raw(
        colors=["blue"],
        shapes=["round"],
        has_price_filter=True,
        price_currency="INR",
        price_type="lte",
        price_amount=50000,
        price_raw_phrase="under INR 50000",
    ))
    kw = attributes_to_keyword_string(attrs)
    assert "blue" in kw
    assert "round" in kw
    assert "under INR 50000" in kw


def test_merge_preserves_base_price():
    base = "blue&under INR 30000"
    llm = "blue&round&under INR 50000"
    merged = merge_extraction_keywords(base, llm)
    assert "under INR 30000" in merged
    assert "round" in merged
    assert "under INR 50000" not in merged


def test_merge_adds_llm_segments_to_empty_base():
    merged = merge_extraction_keywords("", "blue&round")
    assert merged == "blue&round"


def test_resolve_search_keyword_prefers_model_tool_keyword():
    model_kw = "red&bedroom&above INR 1000000&medium"
    message = "i want red rug above 10 lac medium size for my bedroom"
    assert resolve_search_keyword({"keyword": model_kw}, message) == model_kw


def test_validate_medium_size_category_from_sizes_ft():
    attrs, dropped = validate_extracted_attributes(
        _v2_raw(sizes_ft=["medium"]),
        source_text="medium size red rug",
    )
    assert attrs["size_categories"] == ["medium"]
    assert attrs["sizes_ft"] == []
    assert "size_ft:medium" not in dropped


def test_normalise_keyword_strips_size_category_from_mongo_query():
    pf, clean, _, filters = normalise_keyword("red&medium&under INR 500000")
    assert clean == "red"
    assert filters["size_category"] == {"medium"}
    assert pf is not None
    assert pf["amount"] == 500_000


def test_catalog_keyword_excludes_size_categories():
    attrs, _ = validate_extracted_attributes(
        _v2_raw(colors=["red"], size_categories=["medium"]),
        source_text="red medium rug",
    )
    assert attributes_to_catalog_keyword(attrs) == "red"
    payload = build_search_payload_from_attrs(attrs)
    assert payload["clean_keyword"] == "red"
    assert payload["attribute_filters"]["size_category"] == {"medium"}


def test_oversized_maps_to_oversize_size_category():
    _, clean, _, filters = normalise_keyword("pink&oversized")
    assert clean == "pink"
    assert filters["size_category"] == {"oversize"}


def test_serialise_for_json_converts_sets():
    payload = {"color": {"red"}, "shape": set()}
    out = serialise_for_json(payload)
    assert out == {"color": ["red"], "shape": []}


def test_normalise_after_synthesized_keyword():
    attrs, _ = validate_extracted_attributes(_v2_raw(
        colors=["blue"],
        rooms=["living room"],
    ))
    kw = attributes_to_keyword_string(attrs)
    _, clean, _, filters = normalise_keyword(kw)
    assert clean
    assert filters.get("color") or filters.get("color_exact")
    assert filters.get("room")
