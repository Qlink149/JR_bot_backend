import re

from qlink_chatbot.utils.jr_search_aliases import ROUND_SIZE_PATTERN, SIZE_PATTERN

CM_PER_FT = 30.48
CM_PER_IN = 2.54
CM_SIZE_TOLERANCE = 8

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
