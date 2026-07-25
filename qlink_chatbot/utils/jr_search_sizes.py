import math
import re

from qlink_chatbot.utils.jr_search_aliases import (
    DIA_ROUND_PATTERN,
    ROUND_SIZE_PATTERN,
    SIZE_PATTERN,
)


def size_field_regex(size_text: str) -> str:
    """Regex for matching a size chip against SizeInFT / SizeInCM text."""
    round_m = ROUND_SIZE_PATTERN.search(size_text.strip())
    if round_m:
        diameter = round_m.group(1)
        return rf"(?<!\d){diameter}\s*['′]?\s*round\b"
    m = SIZE_PATTERN.search(size_text.strip())
    if not m:
        return re.escape(size_text.strip())
    a, b = m.group(1), m.group(2)
    return rf"(?<!\d){a}\s*['′]?\s*[xX*]\s*{b}(?!\d)"

CM_PER_FT = 30.48
CM_PER_IN = 2.54
CM_SIZE_TOLERANCE = 8

# Website SizeGroup chips (/in/rugs SIZE ft filters) for bucket words.
# Medium is site chips 5×8 / 6×9 / 8×10 — NOT invented sqft bands
# (legacy 48–120 sqft wrongly classified 5×8 = 40 sqft as small).
SIZE_CATEGORY_SIZE_GROUPS: dict[str, frozenset[str]] = {
    "small": frozenset({
        "2x3",
        "3x5",
        "4x6",
        "3x3",
        "4x4",
        "5x5",
        "3diaround",
        "4diaround",
        "5diaround",
    }),
    "medium": frozenset({
        "5x8",
        "6x9",
        "8x10",
        "6x6",
        "7x7",
        "6diaround",
        "7diaround",
        "8diaround",
    }),
    "large": frozenset({
        "9x12",
        "10x14",
        "9x9",
        "9diaround",
        "10diaround",
    }),
    "oversize": frozenset({
        "12x15",
        "12x12",
        "12diaround",
    }),
}

# Last-resort area bands only when SizeGroup / SizeInFT chips are absent.
# Kept for odd catalog rows; primary match is SIZE_CATEGORY_SIZE_GROUPS.
SIZE_CATEGORY_SQFT_RANGES: dict[str, tuple[float, float]] = {
    "small": (0.0, 40.0),
    "medium": (40.0, 90.0),
    "large": (90.0, 160.0),
    "oversize": (160.0, float("inf")),
}

FT_INCH_DIM = re.compile(
    r"(\d+)\s*['′]\s*(\d+)?|(\d+)\s*['′]?(?=[xX*]|$)",
    re.IGNORECASE,
)


def is_cm_dimensions(a: int, b: int, text: str = "") -> bool:
    lowered = (text or "").lower()
    if "cm" in lowered or "centimeter" in lowered or "centimetre" in lowered:
        return True
    return max(a, b) >= 50


def parse_cm_field(value: str) -> tuple[int, int] | None:
    text = (value or "").upper().replace(" ", "")
    if not text or "ROUND" in text:
        return None
    parts = re.split(r"[xX*]", text)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _ft_inches_to_cm(feet: int, inches: int = 0) -> float:
    return feet * CM_PER_FT + inches * CM_PER_IN


def parse_ft_part(part: str) -> float | None:
    part = (part or "").strip().lower()
    match = re.match(r"(\d+)\s*['′]\s*(\d+)?", part)
    if match:
        return _ft_inches_to_cm(int(match.group(1)), int(match.group(2) or 0))
    match = re.match(r"(\d+)\s*$", part)
    if match:
        return _ft_inches_to_cm(int(match.group(1)), 0)
    return None


def parse_ft_field_to_cm(value: str) -> tuple[float, float] | None:
    text = (value or "").strip()
    if not text or ROUND_SIZE_PATTERN.search(text):
        return None
    parts = re.split(r"[xX*]", text.replace(" ", ""))
    if len(parts) != 2:
        return None
    first = parse_ft_part(parts[0])
    second = parse_ft_part(parts[1])
    if first is None or second is None:
        return None
    return first, second


def cm_pair_matches(
    target_w: int,
    target_h: int,
    actual_w: float | int,
    actual_h: float | int,
    *,
    tolerance: int = CM_SIZE_TOLERANCE,
) -> bool:
    aw, ah = float(actual_w), float(actual_h)
    tw, th = float(target_w), float(target_h)
    return (
        (abs(aw - tw) <= tolerance and abs(ah - th) <= tolerance)
        or (abs(aw - th) <= tolerance and abs(ah - tw) <= tolerance)
    )


def parse_requested_cm_size(text: str) -> tuple[int, int] | None:
    cleaned = re.sub(r"\bcm\b", "", (text or ""), flags=re.I).strip()
    match = SIZE_PATTERN.search(cleaned)
    if not match:
        return None
    a, b = int(match.group(1)), int(match.group(2))
    if not is_cm_dimensions(a, b, cleaned):
        return None
    return a, b


def normalise_cm_size_term(a: int, b: int) -> str:
    return f"{a}x{b}"


def _cm_to_ft_inches(cm: float) -> tuple[int, int]:
    total_inches = round(cm / CM_PER_IN)
    feet = total_inches // 12
    inches = total_inches % 12
    if inches == 12:
        feet += 1
        inches = 0
    return int(feet), int(inches)


def cm_to_ft_keyword_variants(width_cm: int, height_cm: int) -> list[str]:
    """Approximate ft size strings used in SizeInFT for Mongo token search."""
    w_ft, w_in = _cm_to_ft_inches(width_cm)
    h_ft, h_in = _cm_to_ft_inches(height_cm)
    variants = {
        f"{w_ft}'{w_in}x{h_ft}'{h_in}",
        f"{h_ft}'{h_in}x{w_ft}'{w_in}",
        f"{w_ft}'{w_in}X{h_ft}'{h_in}",
        f"{h_ft}'{h_in}X{w_ft}'{w_in}",
        f"{w_ft}x{h_ft}",
        f"{h_ft}x{w_ft}",
    }
    if w_in == 0 and h_in == 0:
        variants.update({f"{w_ft}x{h_ft}", f"{h_ft}x{w_ft}"})
    return [v for v in variants if v]


def product_matches_cm_size(product: dict, size_term: str, *, tolerance: int = CM_SIZE_TOLERANCE) -> bool:
    target = parse_requested_cm_size(size_term)
    if not target:
        return False
    target_w, target_h = target

    cm_dims = parse_cm_field(str(product.get("SizeInCM") or ""))
    if cm_dims and cm_pair_matches(target_w, target_h, cm_dims[0], cm_dims[1], tolerance=tolerance):
        return True

    ft_dims = parse_ft_field_to_cm(str(product.get("SizeInFT") or ""))
    if ft_dims and cm_pair_matches(target_w, target_h, ft_dims[0], ft_dims[1], tolerance=tolerance):
        return True
    return False


def product_sqft_from_ft_field(value: str) -> float | None:
    """Estimate rug area in square feet from SizeInFT / SizeGroup round labels."""
    dims_cm = parse_ft_field_to_cm(value)
    if dims_cm:
        return (dims_cm[0] / CM_PER_FT) * (dims_cm[1] / CM_PER_FT)
    text = (value or "").strip()
    if not text:
        return None
    # Round / dia-round: area = π * (d/2)^2 in sq ft.
    dia_m = DIA_ROUND_PATTERN.search(text) or ROUND_SIZE_PATTERN.search(text)
    if dia_m:
        try:
            diameter_ft = float(dia_m.group(1))
        except (TypeError, ValueError):
            return None
        if diameter_ft <= 0:
            return None
        return math.pi * (diameter_ft / 2.0) ** 2
    text_plain = text.replace("'", "").replace("′", "")
    match = SIZE_PATTERN.search(text_plain)
    if match:
        return float(int(match.group(1)) * int(match.group(2)))
    return None


def normalize_size_group_token(term: str) -> str:
    """Normalize SizeGroupInFT / user size chips for comparison (8x10 ↔ 8X10)."""
    text = (term or "").lower().strip()
    if not text:
        return ""
    text = text.replace("rugs", " ")
    text = text.replace("ft.", " ").replace("ft", " ")
    text = re.sub(r"['′]", "", text)
    text = re.sub(r"\s+", "", text)
    text = text.replace("*", "x")
    return text


def size_group_matches_term(size_group: str, term: str) -> bool:
    """True when SizeGroupInFT equals a user size chip (incl. dia round / runner)."""
    group_key = normalize_size_group_token(size_group)
    term_key = normalize_size_group_token(term)
    if not group_key or not term_key:
        return False
    if group_key == term_key:
        return True
    # "6 round" / "6' round" ↔ "6diaround"
    round_user = re.fullmatch(r"(\d+(?:\.\d+)?)round", term_key)
    if round_user and group_key in {
        f"{round_user.group(1)}diaround",
        f"{round_user.group(1)}round",
    }:
        return True
    dia_user = re.fullmatch(r"(\d+(?:\.\d+)?)diaround", term_key)
    if dia_user and group_key in {
        f"{dia_user.group(1)}diaround",
        f"{dia_user.group(1)}round",
    }:
        return True
    return False


def product_matches_size_term(product: dict, term: str) -> bool:
    """Match SizeInFT/CM regex or SizeGroupInFT chip equality."""
    term_text = str(term or "").strip()
    if not term_text:
        return False
    pattern = size_field_regex(term_text)
    for field in ("SizeInFT", "SizeInCM"):
        if re.search(pattern, str(product.get(field) or ""), re.IGNORECASE):
            return True
    if size_group_matches_term(str(product.get("SizeGroupInFT") or ""), term_text):
        return True
    if size_group_matches_term(str(product.get("SizeGroupInCM") or ""), term_text):
        return True
    return False


def _chip_key_to_term(chip_key: str) -> str:
    """Turn normalized chip keys back into matchable size terms."""
    key = (chip_key or "").strip().lower()
    dia = re.fullmatch(r"(\d+(?:\.\d+)?)diaround", key)
    if dia:
        return f"{dia.group(1)} dia round"
    round_m = re.fullmatch(r"(\d+(?:\.\d+)?)round", key)
    if round_m:
        return f"{round_m.group(1)} round"
    return key


def product_matches_size_category(product: dict, category: str) -> bool:
    """Match small/medium/large/oversize via website SizeGroup chips (not sqft-first)."""
    cat = (category or "").lower().strip()
    chips = SIZE_CATEGORY_SIZE_GROUPS.get(cat)
    if not chips:
        return False

    # Site "Oversize Rugs" SizeGroup chip — label beats chip set / sqft.
    if cat == "oversize":
        size_group = str(product.get("SizeGroupInFT") or "").lower()
        if "oversize" in size_group:
            return True

    for field in ("SizeGroupInFT", "SizeGroupInCM"):
        group = str(product.get(field) or "").strip()
        if not group:
            continue
        group_key = normalize_size_group_token(group)
        if group_key in chips:
            return True
        for chip in chips:
            if size_group_matches_term(group, _chip_key_to_term(chip)):
                return True

    # SizeInFT / SizeInCM that resolve to a chip in this bucket (e.g. 5'0x8'0 → 5x8).
    for chip in chips:
        if product_matches_size_term(product, _chip_key_to_term(chip)):
            return True

    # Last resort: area band when catalog has no usable SizeGroup / size chip.
    has_group = bool(
        str(product.get("SizeGroupInFT") or "").strip()
        or str(product.get("SizeGroupInCM") or "").strip()
    )
    if has_group:
        return False
    bounds = SIZE_CATEGORY_SQFT_RANGES.get(cat)
    if not bounds:
        return False
    for field in ("SizeInFT",):
        sqft = product_sqft_from_ft_field(str(product.get(field) or ""))
        if sqft is not None:
            lo, hi = bounds
            return lo <= sqft < hi
    return False
