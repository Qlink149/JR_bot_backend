"""Unit tests for catalog-first color search (no Mongo/OpenAI required)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qlink_chatbot.utils.jr_search_aliases import color_search_terms
from qlink_chatbot.utils.jr_search_mongo import (
    apply_search_pipeline,
    filters_without_size,
    is_mixture_color_intent,
    keyword_has_mixture_colors,
    product_matches_catalog_color,
    product_matches_mixture_colors,
    product_matches_single_color_family,
    segment_to_mongo_clause,
    segment_uses_breakdown_clause,
    strip_size_segments_from_keyword,
)
from qlink_chatbot.utils.jr_search_recommendation import (
    compute_color_match_score,
    select_top_products,
)


def _rug(**kwargs):
    base = {
        "SKU": "TEST-1",
        "BarCode": "RUG1",
        "GrColor": "",
        "BrColor": "",
        "ColorFamily": "",
        "DisplayFilter": "",
        "SizeInFT": "8x10",
        "Shape": "Rectangle",
        "Material": "Wool",
        "Construction": "Hand Knotted",
        "ProductURL": "test-rug",
        "HeadShot": "https://example.com/a.jpg",
    }
    base.update(kwargs)
    return base


def test_segment_uses_breakdown_clause_disabled():
    assert segment_uses_breakdown_clause("purple") is False


def test_strip_size_segments():
    assert strip_size_segments_from_keyword("purple&12x15") == "purple"
    assert strip_size_segments_from_keyword("blue&round&8x10") == "blue&round"


def test_mixture_intent():
    assert is_mixture_color_intent({"color_exact": {"pink", "purple"}}) is True
    assert is_mixture_color_intent({"color_exact": {"purple"}}) is False
    assert is_mixture_color_intent({"multicolor": {"multicolor"}}) is True
    assert keyword_has_mixture_colors("pink&purple&12x15") is True
    assert keyword_has_mixture_colors("purple&12x15") is False


def test_mixture_mongo_clause_includes_colorfamily_for_strict_pink():
    """Strict pink alone excludes ColorFamily; mixture mode includes it."""
    single = segment_to_mongo_clause("pink", mixture_mode=False)
    mixture = segment_to_mongo_clause("pink", mixture_mode=True)
    single_s = str(single)
    mixture_s = str(mixture)
    assert "ColorFamily" not in single_s or "raw.ColorFamily" not in single_s
    # Strict single pink should not open ColorFamily recall.
    assert "raw.ColorFamily" not in single_s
    assert "raw.ColorFamily" in mixture_s


def test_mixture_requires_each_exact_color():
    rose = _rug(GrColor="Rose Smoke", ColorFamily="Pink and Purple")
    purple_only = _rug(GrColor="Dark Purple", ColorFamily="Purple")
    assert product_matches_mixture_colors(rose, {"pink", "purple"})
    assert not product_matches_mixture_colors(purple_only, {"pink", "purple"})


def test_catalog_color_matches_grcolor_alias():
    terms = set(color_search_terms("purple"))
    assert product_matches_catalog_color(
        _rug(GrColor="Dark Purple"), terms
    )
    assert product_matches_catalog_color(
        _rug(GrColor="Continental Plum"), terms
    )
    assert not product_matches_catalog_color(
        _rug(GrColor="Rose Smoke", ColorFamily="Pink and Purple"), terms
    )


def test_single_color_rejects_mixture_family():
    terms = set(color_search_terms("purple"))
    assert product_matches_single_color_family(
        _rug(ColorFamily="Purple"), terms
    )
    assert not product_matches_single_color_family(
        _rug(ColorFamily="Pink and Purple"), terms
    )


def test_pipeline_prefers_catalog_over_pink_purple_family():
    rose = _rug(
        SKU="ROSE-1",
        GrColor="Rose Smoke",
        ColorFamily="Pink and Purple",
        SizeInFT="12x15",
    )
    dark = _rug(
        SKU="DARK-1",
        GrColor="Dark Purple",
        ColorFamily="Purple",
        SizeInFT="12x15",
    )
    results, tier, meta = apply_search_pipeline(
        [rose, dark],
        color_check_terms=set(color_search_terms("purple")),
        attribute_filters={
            "color_exact": {"purple"},
            "color": set(color_search_terms("purple")),
            "size": {"12x15"},
        },
        price_filter=None,
        exclude_skus=None,
    )
    assert tier == "exact_catalog_color"
    assert [p["SKU"] for p in results] == ["DARK-1"]
    assert meta.get("size_relaxed") is False


def test_pipeline_mixture_allows_pink_and_purple_family():
    rose = _rug(
        SKU="ROSE-1",
        GrColor="Rose Smoke",
        ColorFamily="Pink and Purple",
        SizeInFT="12x15",
    )
    results, tier, meta = apply_search_pipeline(
        [rose],
        color_check_terms=set(color_search_terms("purple")) | set(color_search_terms("pink")),
        attribute_filters={
            "color_exact": {"pink", "purple"},
            "size": {"12x15"},
        },
        price_filter=None,
        exclude_skus=None,
    )
    assert tier == "mixture_catalog_color"
    assert len(results) == 1
    assert results[0]["SKU"] == "ROSE-1"


def test_rank_catalog_beats_high_pct_without_label():
    """GrColor purple should rank above unlabeled high yarn-% (score only)."""
    labeled = _rug(SKU="LAB", GrColor="Tulip Purple", SizeInFT="6x9")
    unlabeled = _rug(SKU="YARN", GrColor="Rose Smoke", SizeInFT="12x15")
    breakdown = {
        "LAB": [{"color": "Purple", "percentage": 40.0}],
        "YARN": [{"color": "Purple", "percentage": 90.0}, {"color": "Pink", "percentage": 10.0}],
    }
    terms = set(color_search_terms("purple"))
    selected, scores = select_top_products(
        [unlabeled, labeled],
        match_terms=terms,
        exact_color_terms={"purple"},
        breakdown_by_sku=breakdown,
        color_search_tier="exact_catalog_color",
        size_terms={"12x15"},
        size_relaxed=True,
        limit=2,
    )
    assert selected[0]["SKU"] == "LAB"
    assert scores[0]["catalog_score"] >= 1


def test_filters_without_size():
    out = filters_without_size(
        {"color_exact": {"purple"}, "size": {"12x15"}, "size_cm": {"360x450"}, "shape": {"round"}}
    )
    assert "size" not in out
    assert "size_cm" not in out
    assert out["color_exact"] == {"purple"}
    assert out["shape"] == {"round"}


def test_compute_score_sole_dominant():
    product = _rug(GrColor="Dark Purple")
    score = compute_color_match_score(
        product,
        match_terms=set(color_search_terms("purple")),
        exact_color_terms={"purple"},
        breakdown_by_sku={
            "TEST-1": [
                {"color": "Purple", "percentage": 70.0},
                {"color": "Green", "percentage": 30.0},
            ]
        },
    )
    assert score["sole_dominant"] is True
    assert score["closeness_band"] == 3
    assert score["catalog_score"] >= 1


def test_accent_purple_not_sole_dominant():
    """Grey-led rug with purple accent must NOT rank as dominant."""
    product = _rug(SKU="ACCENT", GrColor="Purple Mist")
    score = compute_color_match_score(
        product,
        match_terms=set(color_search_terms("purple")),
        exact_color_terms={"purple"},
        breakdown_by_sku={
            "ACCENT": [
                {"color": "Grey", "percentage": 65.0},
                {"color": "Purple", "percentage": 35.0},
            ]
        },
    )
    assert score["sole_dominant"] is False
    assert score["closeness_band"] == 2  # secondary (35%)
    assert score["requested_pct"] == 35.0


def test_nearest_first_dominant_beats_accent_same_label():
    """For any color: yarn-dominant nearest match beats accent with same GrColor word."""
    dominant = _rug(SKU="DOM", GrColor="Ocean Blue")
    accent = _rug(SKU="ACC", GrColor="Blue Mist")
    breakdown = {
        "DOM": [
            {"color": "Blue", "percentage": 75.0},
            {"color": "White", "percentage": 25.0},
        ],
        "ACC": [
            {"color": "Grey", "percentage": 80.0},
            {"color": "Blue", "percentage": 20.0},
        ],
    }
    terms = set(color_search_terms("blue"))
    selected, scores = select_top_products(
        [accent, dominant],
        match_terms=terms,
        exact_color_terms={"blue"},
        breakdown_by_sku=breakdown,
        color_search_tier="exact_catalog_color",
        limit=2,
    )
    assert selected[0]["SKU"] == "DOM"
    assert scores[0]["closeness_band"] > scores[1]["closeness_band"]


def test_nearest_first_labeled_beats_unlabeled_accent():
    """Named color on GrColor beats unlabeled high accent yarn %."""
    labeled = _rug(SKU="LAB", GrColor="Crimson Red")
    yarn_only = _rug(SKU="YARN", GrColor="Sand")
    breakdown = {
        "LAB": [{"color": "Red", "percentage": 55.0}, {"color": "Brown", "percentage": 45.0}],
        "YARN": [{"color": "Red", "percentage": 90.0}, {"color": "White", "percentage": 10.0}],
    }
    # yarn_only has no catalog red label → catalog_score 0 even with high red %
    selected, scores = select_top_products(
        [yarn_only, labeled],
        match_terms=set(color_search_terms("red")),
        exact_color_terms={"red"},
        breakdown_by_sku=breakdown,
        color_search_tier="exact_catalog_color",
        limit=2,
    )
    assert selected[0]["SKU"] == "LAB"
    assert scores[0]["catalog_score"] >= scores[1]["catalog_score"]


def test_border_only_demoted_vs_ground_color():
    ground = _rug(SKU="GND", GrColor="Emerald", BrColor="Ivory")
    border = _rug(SKU="BRD", GrColor="Ivory", BrColor="Emerald Green")
    terms = set(color_search_terms("green"))
    selected, scores = select_top_products(
        [border, ground],
        match_terms=terms,
        exact_color_terms={"green"},
        breakdown_by_sku={},
        color_search_tier="exact_catalog_color",
        limit=2,
    )
    assert selected[0]["SKU"] == "GND"
    assert scores[1].get("border_only") is True or scores[0]["sort_key"] > scores[1]["sort_key"]
