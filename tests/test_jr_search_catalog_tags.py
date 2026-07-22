"""Unit tests for website mega-menu catalog tags + SizeGroup size chips."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qlink_chatbot.utils.jr_search_aliases import (
    CONSTRUCTION_KEYWORDS,
    normalise_catalog_tag,
)
from qlink_chatbot.utils.jr_search_index import build_search_tokens
from qlink_chatbot.utils.jr_search_keywords import normalise_keyword, preprocess_natural_language
from qlink_chatbot.utils.jr_search_mongo import (
    apply_attribute_post_filters,
    classify_segment,
    product_matches_catalog_tag,
)
from qlink_chatbot.utils.jr_search_llm_extract import (
    _attrs_have_searchable_content,
    _sanitize_attrs_to_current_message,
    _should_ignore_previous_search,
    build_search_payload_from_attrs,
)
from qlink_chatbot.utils.jr_search_sizes import (
    product_matches_size_category,
    product_matches_size_term,
    size_group_matches_term,
)


def _rug(**kwargs):
    base = {
        "SKU": "TEST-1",
        "BarCode": "RUG1",
        "GrColor": "Ivory",
        "BrColor": "",
        "ColorFamily": "Ivory",
        "DisplayFilter": "",
        "SizeInFT": "8x10",
        "SizeGroupInFT": "8X10",
        "Shape": "Rectangle",
        "Material": "Wool",
        "MaterialFamilies": "Wool",
        "Construction": "Hand Knotted",
        "Quality": "",
        "ProductTag": "",
        "BestSellerStatus": False,
        "Room": "Living Room",
        "ProductURL": "test-rug",
        "HeadShot": "https://example.com/a.jpg",
    }
    base.update(kwargs)
    return base


def test_catalog_tag_aliases():
    assert normalise_catalog_tag("new arrival") == "new"
    assert normalise_catalog_tag("bestsellers") == "bestseller"
    assert normalise_catalog_tag("outdoor rugs") == "outdoor"
    assert normalise_catalog_tag("antique") == "antique"
    assert normalise_catalog_tag("rug swatch") == "swatch"


def test_outdoor_not_room_in_preprocess():
    parsed = preprocess_natural_language("outdoor rugs")
    assert "outdoor" in parsed
    assert "living room" not in parsed


def test_normalise_keyword_catalog_tags():
    for phrase, tag in (
        ("new arrival rugs", "new"),
        ("bestsellers", "bestseller"),
        ("antique rugs", "antique"),
        ("rug swatch", "swatch"),
        ("outdoor rugs", "outdoor"),
    ):
        _pf, clean, _colors, attrs = normalise_keyword(phrase)
        assert tag in (attrs.get("catalog_tag") or set()), phrase
        assert classify_segment(tag) == "catalog_tag"
        assert tag in clean.split("&")


def test_product_matches_catalog_tags():
    assert product_matches_catalog_tag(_rug(ProductTag="New"), "new")
    assert product_matches_catalog_tag(_rug(BestSellerStatus=True), "bestseller")
    assert product_matches_catalog_tag(_rug(ProductTag="bestseller"), "bestseller")
    assert product_matches_catalog_tag(_rug(ProductTag="Outdoor"), "outdoor")
    assert product_matches_catalog_tag(_rug(Quality="Antique"), "antique")
    assert product_matches_catalog_tag(_rug(SizeGroupInFT="SWATCHES"), "swatch")
    assert not product_matches_catalog_tag(_rug(ProductTag=""), "outdoor")


def test_catalog_tag_hard_post_filter():
    products = [
        _rug(SKU="N1", ProductTag="New"),
        _rug(SKU="O1", ProductTag="Outdoor"),
        _rug(SKU="B1", BestSellerStatus=True),
    ]
    filtered = apply_attribute_post_filters(
        products,
        {"catalog_tag": {"outdoor"}},
    )
    assert [p["SKU"] for p in filtered] == ["O1"]


def test_size_group_chip_match():
    assert size_group_matches_term("8X10", "8x10")
    assert product_matches_size_term(_rug(SizeInFT="8'0x10'0", SizeGroupInFT="8X10"), "8x10")
    assert product_matches_size_term(
        _rug(SizeInFT="", SizeGroupInFT="6 Dia Round"),
        "6 dia round",
    )


def test_oversize_size_category_uses_size_group():
    product = _rug(SizeInFT="2x3", SizeGroupInFT="Oversize Rugs")
    assert product_matches_size_category(product, "oversize")


def test_shag_in_construction_keywords():
    assert "shag" in CONSTRUCTION_KEYWORDS
    _pf, clean, _c, attrs = normalise_keyword("shag rugs")
    assert "shag" in (attrs.get("construction") or set())


def test_build_search_tokens_includes_merch_facets():
    tokens = build_search_tokens(
        _rug(ProductTag="New", BestSellerStatus=True, Quality="Antique", SizeGroupInFT="SWATCHES")
    )
    assert "new" in tokens
    assert "bestseller" in tokens
    assert "antique" in tokens
    assert "swatch" in tokens or "swatches" in tokens


def test_shop_by_combo_extract():
    _pf, clean, colors, attrs = normalise_keyword(
        "purple 8x10 wool living room hand knotted"
    )
    assert "purple" in (attrs.get("color_exact") or set())
    assert "8x10" in (attrs.get("size") or set())
    assert "wool" in (attrs.get("material") or set())
    assert "living room" in (attrs.get("room") or set())
    assert "hand knotted" in (attrs.get("construction") or set())


def test_fresh_new_arrival_ignores_previous_search():
    assert _should_ignore_previous_search("new arrival", "new arrival rugs") is True
    assert _should_ignore_previous_search("bestsellers", "bestsellers") is True
    # Refinement language keeps previous context.
    assert _should_ignore_previous_search(
        "blue", "same but in blue"
    ) is False


def test_sanitize_strips_previous_bleed_from_new_arrival():
    polluted = {
        "colors": ["purple"],
        "shapes": [],
        "sizes_ft": ["6x9"],
        "sizes_cm": [],
        "size_categories": [],
        "materials": ["wool", "tencil"],
        "constructions": [],
        "patterns": [],
        "rooms": [],
        "catalog_tags": ["new"],
        "multicolor": False,
        "weight_max_kg": None,
        "price": None,
        "sku": None,
        "collection": None,
        "refinement": "refine_previous",
    }
    clean = _sanitize_attrs_to_current_message(
        polluted,
        keyword="new arrival",
        user_message="new arrival rugs",
    )
    assert clean["catalog_tags"] == ["new"]
    assert clean["colors"] == []
    assert clean["sizes_ft"] == []
    assert clean["materials"] == []
    assert clean["refinement"] == "new"
    assert _attrs_have_searchable_content(clean) is True
    payload = build_search_payload_from_attrs(clean)
    assert payload["clean_keyword"] == "new"
    assert payload["attribute_filters"]["catalog_tag"] == {"new"}
    assert not payload["attribute_filters"].get("color_exact")
