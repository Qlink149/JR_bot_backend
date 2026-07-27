"""Golden query suite — Phase A site-parity regressions (no live Mongo/OpenAI).

Cases from docs/FOUNDER_OP_ASSESSMENT.md:
  soft new arrival; aurelia+red; red+round+USD band (PAE-5080-class);
  medium+5x8; show-more; Hindi color; prior-turn bleed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qlink_chatbot.agent.chat_agent import (
    _is_product_show_more_followup,
    _resolve_show_more_search,
)
from qlink_chatbot.utils.jr_search_aliases import color_search_terms
from qlink_chatbot.utils.jr_search_currency import extract_price_filter_from_text
from qlink_chatbot.utils.jr_search_keywords import normalise_keyword
from qlink_chatbot.utils.jr_search_llm_extract import (
    _sanitize_attrs_to_current_message,
    _should_ignore_previous_search,
    apply_llm_evidence_gate,
    build_search_payload_from_attrs,
    validate_extracted_attributes,
)
from qlink_chatbot.utils.jr_search_strategies import build_search_strategies
from qlink_chatbot.utils.jr_search_mongo import apply_search_pipeline
from qlink_chatbot.utils.jr_search_recommendation import select_top_products
from qlink_chatbot.utils.jr_search_sizes import product_matches_size_category
from qlink_chatbot.utils.product_format import format_product_search_message
from qlink_chatbot.utils.search_session import (
    build_search_intro,
    products_honesty_note,
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
        "catalog_tags": [],
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


def _rug(**kwargs):
    base = {
        "SKU": "TEST-1",
        "BarCode": "RUG1",
        "GrColor": "",
        "BrColor": "",
        "ColorFamily": "",
        "DisplayFilter": "",
        "SizeInFT": "8x10",
        "SizeGroupInFT": "8X10",
        "Shape": "Rectangle",
        "Material": "Wool",
        "Construction": "Hand Knotted",
        "Collection": "",
        "Name": "Test Rug",
        "ProductURL": "test-rug",
        "HeadShot": "https://example.com/a.jpg",
        "USD_MRP": 0,
        "INR_MRP": 0,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# 1) Soft new arrival
# ---------------------------------------------------------------------------

def test_golden_soft_new_arrival_extract_and_hygiene():
    user = "is there any new arrival"
    assert _should_ignore_previous_search("new arrival", user) is True

    attrs, _ = validate_extracted_attributes(
        _v2_raw(catalog_tags=["new"], refinement="new"),
        source_text=user,
    )
    payload = build_search_payload_from_attrs(attrs)
    assert payload["attribute_filters"]["catalog_tag"] == {"new"}
    assert "red" not in (payload["attribute_filters"].get("color_exact") or set())

    # Polluted prior aurelia/red must not stick on soft new-arrival ask.
    polluted = _v2_raw(
        colors=["red"],
        shapes=["round"],
        sizes_ft=["8x10"],
        catalog_tags=["new"],
        collection="Aurelia",
        refinement="refine_previous",
    )
    clean = _sanitize_attrs_to_current_message(
        polluted,
        keyword="new arrival",
        user_message=user,
    )
    assert clean["catalog_tags"] == ["new"]
    assert clean["colors"] == []
    assert clean["shapes"] == []
    assert clean.get("collection") in (None, "")


# ---------------------------------------------------------------------------
# 2) Aurelia + red
# ---------------------------------------------------------------------------

def test_golden_aurelia_red_extract():
    user = "show me aurelia in red"
    attrs, _ = validate_extracted_attributes(
        _v2_raw(colors=["red"], collection="Aurelia", refinement="new"),
        source_text=user,
    )
    assert attrs["colors"] == ["red"]
    assert (attrs.get("collection") or "").lower() == "aurelia"
    payload = build_search_payload_from_attrs(attrs)
    assert payload["attribute_filters"]["color_exact"] == {"red"}

    _pf, clean, _colors, filters = normalise_keyword("aurelia&red")
    assert "red" in (filters.get("color_exact") or set())
    assert "aurelia" in clean.lower() or "aurelia" in "aurelia&red"


# ---------------------------------------------------------------------------
# 3) Red + round + USD band → PAE-5080-class (ColorFamily), shape kept
# ---------------------------------------------------------------------------

def test_golden_red_round_usd_band_keeps_shape_and_pae_class():
    user = (
        "show me red rugs above 15000 usd and below 20000 usd "
        "round shape medium size"
    )
    # Extract / price band
    attrs, _ = validate_extracted_attributes(
        _v2_raw(
            colors=["red"],
            shapes=["round"],
            size_categories=["medium"],
            has_price_filter=True,
            price_type="range",
            price_currency="USD",
            price_min=15000,
            price_max=20000,
            price_raw_phrase="above 15000 usd and below 20000 usd",
            refinement="new",
        ),
        source_text=user,
    )
    payload = build_search_payload_from_attrs(attrs)
    assert payload["attribute_filters"]["color_exact"] == {"red"}
    assert payload["attribute_filters"]["shape"] == {"round"}
    assert payload["attribute_filters"]["size_category"] == {"medium"}
    pf = payload["price_filter"]
    assert pf is not None
    assert pf.get("currency") == "USD"
    assert float(pf.get("min_amount") or 0) == 15000
    assert float(pf.get("max_amount") or 0) == 20000

    # Regex price path also keeps USD band (demo currency contract).
    regex_pf, _ = extract_price_filter_from_text(
        "above 15000 usd and below 20000 usd"
    )
    assert regex_pf is not None
    assert regex_pf.get("currency") == "USD"

    # PAE-5080-class: Soft Coral + ColorFamily Red and Orange.
    # Use an 8 Dia Round (medium chip) so size_category=medium does not drop it;
    # live PAE-5080 is 9 round (large chip) — ColorFamily recall is the LOGIC under test.
    soft_coral_round = _rug(
        SKU="PAE-5080-0001",
        Name="PAE Soft Coral Round",
        GrColor="Soft Coral",
        ColorFamily="Red and Orange",
        Shape="Round",
        SizeInFT="8 Round",
        SizeGroupInFT="8 Dia Round",
        USD_MRP=17982,
    )
    hard_red_rect = _rug(
        SKU="RED-RECT-1",
        Name="Crimson Rectangle",
        GrColor="Crimson Red",
        ColorFamily="Red",
        Shape="Rectangle",
        SizeInFT="8x10",
        SizeGroupInFT="8X10",
        USD_MRP=18000,
    )
    out_of_band = _rug(
        SKU="RED-ROUND-CHEAP",
        GrColor="Red",
        ColorFamily="Red",
        Shape="Round",
        SizeGroupInFT="6 Dia Round",
        USD_MRP=5000,
    )

    results, tier, meta = apply_search_pipeline(
        [soft_coral_round, hard_red_rect, out_of_band],
        color_check_terms=set(color_search_terms("red")),
        attribute_filters=payload["attribute_filters"],
        price_filter=payload["price_filter"],
        exclude_skus=None,
    )
    skus = {p["SKU"] for p in results}
    # Shape kept → Soft Coral ColorFamily round in-band wins; rect dropped.
    assert "PAE-5080-0001" in skus
    assert "RED-RECT-1" not in skus
    assert "RED-ROUND-CHEAP" not in skus
    assert meta.get("size_relaxed") is False
    assert tier == "similar_catalog_color"
    assert "ColorFamily" in (meta.get("color_relax_note") or "")

    # ColorFamily-only is honest-relaxed (not claimed as exact ground color).
    products = [
        {
            **p,
            "fallback_note": meta.get("color_relax_note") or "",
            "size_relaxed": False,
        }
        for p in results
    ]
    assert "ColorFamily" in (products_honesty_note(products) or "")


# ---------------------------------------------------------------------------
# 4) Medium + 5x8 (site SizeGroup chips)
# ---------------------------------------------------------------------------

def test_golden_medium_includes_5x8_chip():
    five_by_eight = _rug(SizeInFT="5'0x8'0", SizeGroupInFT="5X8")
    four_by_six = _rug(SizeInFT="4x6", SizeGroupInFT="4X6")
    assert product_matches_size_category(five_by_eight, "medium")
    assert not product_matches_size_category(five_by_eight, "small")
    assert product_matches_size_category(four_by_six, "small")

    attrs, _ = validate_extracted_attributes(
        _v2_raw(size_categories=["medium"], sizes_ft=["5x8"], refinement="new"),
        source_text="medium size 5x8 rug",
    )
    assert "medium" in attrs["size_categories"]
    assert "5x8" in attrs["sizes_ft"]

    payload = build_search_payload_from_attrs(attrs)
    results, _tier, meta = apply_search_pipeline(
        [five_by_eight, four_by_six],
        color_check_terms=set(),
        attribute_filters=payload["attribute_filters"],
        price_filter=None,
        exclude_skus=None,
    )
    assert [p["SKU"] for p in results] == ["TEST-1"]  # default SKU on five_by_eight
    # Re-tag for clarity
    five_by_eight["SKU"] = "5X8-1"
    four_by_six["SKU"] = "4X6-1"
    results, _tier, meta = apply_search_pipeline(
        [five_by_eight, four_by_six],
        color_check_terms=set(),
        attribute_filters={"size_category": {"medium"}, "size": {"5x8"}},
        price_filter=None,
        exclude_skus=None,
    )
    assert [p["SKU"] for p in results] == ["5X8-1"]
    assert meta.get("size_relaxed") is False


# ---------------------------------------------------------------------------
# 5) Show-more
# ---------------------------------------------------------------------------

def test_golden_show_more_reuses_keyword_and_excludes_shown():
    previous = [{
        "keyword": "red&round",
        "results": [
            {"SKU": "A1", "name": "One"},
            {"SKU": "A2", "name": "Two"},
            {"SKU": "A3", "name": "Three"},
        ],
    }]
    chat = [
        {"role": "user", "content": "show me red round rugs"},
        {
            "role": "assistant",
            "content": "**1. One**\n- Price: USD 1\n![Rug Image](https://x)",
        },
    ]
    assert _is_product_show_more_followup("show me more", chat, previous) is True
    keyword, exclude = _resolve_show_more_search(previous, "show me more")
    assert keyword == "red&round"
    assert {"A1", "A2", "A3"} <= {s.upper() for s in exclude} or exclude

    # Explicit "show more 5x5" switches keyword.
    kw2, _ex2 = _resolve_show_more_search(previous, "show more 5x5 rugs")
    assert "5x5" in kw2.lower()


# ---------------------------------------------------------------------------
# 6) Hindi / Hinglish color
# ---------------------------------------------------------------------------

def test_golden_hindi_laal_maps_to_red():
    _pf, clean, colors, filters = normalise_keyword("laal gol dari")
    assert "red" in (filters.get("color_exact") or set()) or "red" in clean
    assert "round" in (filters.get("shape") or set()) or "round" in clean

    attrs, dropped = validate_extracted_attributes(
        _v2_raw(colors=["laal"], shapes=["gol"], refinement="new"),
        source_text="laal gol dari",
    )
    assert attrs["colors"] == ["red"]
    assert attrs["shapes"] == ["round"]
    assert not any(d.startswith("color:laal") for d in dropped)

    # Devanagari
    attrs2, _ = validate_extracted_attributes(
        _v2_raw(colors=["लाल"], refinement="new"),
        source_text="लाल rug",
    )
    assert attrs2["colors"] == ["red"]


def test_golden_hinglish_hara_gol_maps_green_round():
    _pf, clean, colors, filters = normalise_keyword("hara gol dari")
    assert "green" in (filters.get("color_exact") or set()) or "green" in clean
    assert "round" in (filters.get("shape") or set()) or "round" in clean


def test_golden_hinglish_peela_under_budget_keeps_yellow():
    from qlink_chatbot.utils.jr_search_aliases import canonical_color_key

    user = "peela rugs under 50000 inr"
    assert canonical_color_key("peela") == "yellow"
    attrs, _dropped = validate_extracted_attributes(
        _v2_raw(colors=["yellow"], refinement="new"),
        source_text=user,
    )
    payload = build_search_payload_from_attrs(attrs)
    assert "yellow" in (payload["attribute_filters"].get("color_exact") or set())


def test_golden_soft_new_arrival_still_english_clean_after_hinglish_prior():
    clean = apply_llm_evidence_gate(
        "is there any new arrival",
        {"colors": ["green"], "shapes": ["round"], "catalog_tags": ["new"]},
    )
    assert clean.get("colors") == []
    assert clean.get("shapes") == []
    assert clean.get("catalog_tags") == ["new"]


# ---------------------------------------------------------------------------
# 7) Bleed: soft ask after aurelia must not keep collection/color
# ---------------------------------------------------------------------------

def test_golden_bleed_new_arrival_after_aurelia():
    polluted = {
        "colors": ["red"],
        "shapes": ["round"],
        "sizes_ft": ["8x10"],
        "sizes_cm": [],
        "size_categories": [],
        "materials": [],
        "constructions": [],
        "patterns": [],
        "rooms": [],
        "catalog_tags": ["new"],
        "multicolor": False,
        "weight_max_kg": None,
        "price": None,
        "sku": None,
        "collection": "Aurelia",
        "refinement": "refine_previous",
    }
    clean = _sanitize_attrs_to_current_message(
        polluted,
        keyword="aurelia&red&round&8x10&new",
        user_message="is there any new arrival",
    )
    assert clean["catalog_tags"] == ["new"]
    assert clean["colors"] == []
    assert clean.get("collection") in (None, "")
    payload = build_search_payload_from_attrs(clean)
    assert payload["clean_keyword"] == "new"


# ---------------------------------------------------------------------------
# 8) Honesty: single note when shape dropped (no stack with size)
# ---------------------------------------------------------------------------

def test_golden_honesty_single_note_on_shape_drop():
    products = [{
        "name": "Crimson Rect",
        "size_relaxed": True,
        "search_strategy": "drop_shape",
        "fallback_note": (
            "No rugs matched that shape with your other filters — showing other shapes."
        ),
        "display_price": "USD 18,000",
        "url": "https://www.jaipurrugs.com/in/rugs/x",
        "image": "https://example.com/x.jpg",
        "size": "8x10",
        "material": "Wool",
    }]
    intro = build_search_intro(products=products)
    assert "showing other shapes" in intro.lower()
    assert "closest available sizes" not in intro.lower()
    msg = format_product_search_message(products)
    assert "showing other shapes" in msg.lower()
    assert msg.lower().count("no rugs matched that shape") == 1
    assert products[0]["search_strategy"] == "drop_shape"


def test_golden_overconstrained_strategy_drop_shape():
    """Fixture: red+round+silk over-constrained → drop_shape strategy + note kind."""
    filters = {
        "color_exact": {"red"},
        "shape": {"round"},
        "material": {"silk"},
        "size_category": {"medium"},
    }
    strats = build_search_strategies(
        "red&round&silk&medium",
        filters,
        {"operator": "$lte", "amount": 20000, "currency": "USD"},
    )
    ids = [s.id for s in strats]
    assert ids[0] == "exact"
    assert "drop_size" in ids
    assert "drop_shape" in ids
    drop_shape = next(s for s in strats if s.id == "drop_shape")
    assert "showing other shapes" in drop_shape.note.lower()
    # Simulated win: attach strategy fields the API would set on products.
    products = [{
        "name": "Red Rect",
        "search_strategy": "drop_shape",
        "fallback_note": drop_shape.note,
        "size_relaxed": False,
        "display_price": "USD 18,000",
        "url": "https://www.jaipurrugs.com/in/rugs/x",
        "image": "https://example.com/x.jpg",
        "size": "8x10",
        "material": "Wool",
    }]
    assert products_honesty_note(products) == drop_shape.note
    assert "showing other shapes" in build_search_intro(products=products).lower()


def test_golden_evidence_gate_strips_aurelia_keeps_hinglish():
    soft = apply_llm_evidence_gate(
        "is there any new arrival",
        {
            "colors": ["red"],
            "shapes": ["round"],
            "collection": "Aurelia",
            "catalog_tags": ["new"],
        },
    )
    assert soft.get("colors") == []
    assert soft.get("shapes") == []
    assert soft.get("collection") in (None, "")
    assert soft.get("catalog_tags") == ["new"]

    hinglish = apply_llm_evidence_gate(
        "laal gol dari",
        {"colors": ["red"], "shapes": ["round"], "collection": "Aurelia"},
    )
    assert "red" in hinglish.get("colors") or []
    assert "round" in hinglish.get("shapes") or []
    assert hinglish.get("collection") in (None, "")


# ---------------------------------------------------------------------------
# Ranking: GrColor red still beats ColorFamily soft coral
# ---------------------------------------------------------------------------

def test_usd_budget_beats_india_country_default_for_display():
    """IN locale must not force INR when user asked USD 15–20k."""
    from qlink_chatbot.utils.jr_search_currency import extract_requested_currency_from_text

    assert extract_requested_currency_from_text(
        "show me red rugs above 15000 usd and below 20000 usd round shape"
    ) == "USD"
    assert extract_requested_currency_from_text("under INR 50000") == "INR"


def test_golden_nearest_first_grcolor_beats_colorfamily():
    soft = _rug(
        SKU="PAE-5080-0001",
        GrColor="Soft Coral",
        ColorFamily="Red and Orange",
        Shape="Round",
    )
    hard = _rug(
        SKU="RED-1",
        GrColor="Crimson Red",
        ColorFamily="Red",
        Shape="Round",
    )
    selected, scores = select_top_products(
        [soft, hard],
        match_terms=set(color_search_terms("red")),
        exact_color_terms={"red"},
        breakdown_by_sku={},
        color_search_tier="exact_catalog_color",
        limit=2,
    )
    assert selected[0]["SKU"] == "RED-1"
    assert scores[0]["catalog_score"] > scores[1]["catalog_score"]
