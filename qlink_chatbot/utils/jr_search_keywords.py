import re

from qlink_chatbot.utils.jr_search_aliases import (
    COLOR_ALIASES,
    CONSTRUCTION_KEYWORDS,
    MATERIAL_KEYWORDS,
    NOISE_WORDS,
    PATTERN_ALIASES,
    ROOM_KEYWORDS,
    SHAPE_ALIASES,
    SIZE_PATTERN,
    WEIGHT_PATTERN,
)
from qlink_chatbot.utils.jr_search_currency import extract_price_filter_from_text


def expand_term(segment: str, *, multi_attribute: bool = False) -> str:
    """Expand a keyword segment for the JR API.

    Colors are sent as the user's exact term (e.g. ``pink``), not expanded to
    related shades. Explicit ``||`` in the query is preserved. Shapes and
    patterns still map to catalog values.
    """
    if "||" in segment:
        expanded = []
        for part in segment.split("||"):
            key = part.strip().lower()
            if key in SHAPE_ALIASES:
                expanded.append(SHAPE_ALIASES[key])
            elif key in PATTERN_ALIASES:
                expanded.append(PATTERN_ALIASES[key])
            elif key in COLOR_ALIASES and "||" not in COLOR_ALIASES[key]:
                expanded.append(COLOR_ALIASES[key])
            else:
                expanded.append(part.strip())
        return "||".join(expanded)

    key = segment.strip().lower()
    if key in SHAPE_ALIASES:
        return SHAPE_ALIASES[key]
    if key in PATTERN_ALIASES:
        return PATTERN_ALIASES[key]
    if key in COLOR_ALIASES and "||" not in COLOR_ALIASES[key]:
        return COLOR_ALIASES[key]
    return segment.strip()


def preprocess_natural_language(keyword: str) -> str:
    """Convert natural language like 'show me blue round rugs' → 'blue&round'."""
    text = (keyword or "").lower().strip()
    if "&" in text or "||" in text:
        return keyword.strip()

    text = SIZE_PATTERN.sub(lambda m: f"{m.group(1)}x{m.group(2)}", text)
    words = [w for w in re.split(r"[\s,]+", text) if w and w not in NOISE_WORDS]
    if not words:
        return keyword.strip()

    found: list[str] = []
    remaining = " ".join(words)

    for kw in sorted(CONSTRUCTION_KEYWORDS, key=len, reverse=True):
        if kw in remaining:
            found.append(kw)
            remaining = remaining.replace(kw, " ").strip()

    for kw in sorted(MATERIAL_KEYWORDS, key=len, reverse=True):
        if kw in remaining:
            found.append(kw)
            remaining = remaining.replace(kw, " ").strip()

    for alias in sorted(COLOR_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append(alias)
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()

    for alias in sorted(SHAPE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append(alias)
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()

    for alias in sorted(PATTERN_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append(alias)
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()

    for room in sorted(ROOM_KEYWORDS, key=len, reverse=True):
        if room in remaining:
            found.append(room)
            remaining = remaining.replace(room, " ").strip()

    weight_match = WEIGHT_PATTERN.search(remaining)
    if weight_match:
        found.append(weight_match.group(0).replace(" ", ""))
        remaining = remaining[:weight_match.start()] + remaining[weight_match.end():]

    for m in SIZE_PATTERN.finditer(remaining):
        found.append(f"{m.group(1)}x{m.group(2)}")

    if len(found) >= 2:
        return "&".join(found)
    return keyword.strip()


def track_attribute_terms(segment_key: str, attribute_filters: dict[str, set]) -> None:
    key = segment_key.strip().lower()
    if not key:
        return

    if key in COLOR_ALIASES:
        attribute_filters["color"].add(key)
        expansion = COLOR_ALIASES[key]
        if "||" not in expansion:
            attribute_filters["color"].add(expansion.lower())
        return

    if key in SHAPE_ALIASES:
        attribute_filters["shape"].add(SHAPE_ALIASES[key].lower())
        attribute_filters["shape"].add(key)
        return

    if key in PATTERN_ALIASES:
        attribute_filters["pattern"].add(PATTERN_ALIASES[key].lower())
        attribute_filters["pattern"].add(key)
        return

    if SIZE_PATTERN.search(key):
        m = SIZE_PATTERN.search(key)
        if m:
            attribute_filters["size"].add(f"{m.group(1)}x{m.group(2)}".lower())
        return

    for kw in sorted(CONSTRUCTION_KEYWORDS, key=len, reverse=True):
        if kw in key:
            attribute_filters["construction"].add(kw)
            return

    for kw in sorted(MATERIAL_KEYWORDS, key=len, reverse=True):
        if kw in key:
            attribute_filters["material"].add(kw)
            return

    for room in sorted(ROOM_KEYWORDS, key=len, reverse=True):
        if room in key:
            attribute_filters["room"].add(room)
            return

    weight_match = WEIGHT_PATTERN.search(key)
    if weight_match:
        attribute_filters["weight_max"].add(float(weight_match.group(1)))


def normalise_keyword(keyword: str) -> tuple[dict | None, str, set[str], dict[str, set]]:
    keyword = preprocess_natural_language((keyword or "").strip())
    overall_price_filter = None
    pending_segments: list[str] = []
    clean_segments = []
    color_check_terms: set[str] = set()
    attribute_filters: dict[str, set] = {
        "color": set(), "shape": set(), "size": set(),
        "material": set(), "construction": set(), "pattern": set(),
        "room": set(), "weight_max": set(),
    }

    for segment in keyword.split("&"):
        segment = segment.strip().lower()
        if not segment:
            continue

        segment = SIZE_PATTERN.sub(lambda m: f"{m.group(1)}x{m.group(2)}", segment)
        price_filter, residual = extract_price_filter_from_text(segment)
        if price_filter:
            overall_price_filter = price_filter
            segment = residual.strip()

        words = [w for w in segment.split() if w.lower() not in NOISE_WORDS]
        segment = " ".join(words).strip()
        if segment:
            pending_segments.append(segment)

    multi_attribute = len(pending_segments) > 1

    for segment in pending_segments:
        expanded = expand_term(segment, multi_attribute=multi_attribute)
        clean_segments.append(expanded)
        key = segment.strip().lower()
        track_attribute_terms(key, attribute_filters)

        if key in COLOR_ALIASES:
            color_check_terms.add(key)
            expansion = COLOR_ALIASES[key]
            if "||" not in expansion:
                color_check_terms.add(expansion.lower())
            else:
                for part in expansion.split("||"):
                    part_lower = part.strip().lower()
                    # Avoid pink synonyms that false-match names like "Tea Rose".
                    if key == "pink" and part_lower in {"rose", "mauve", "coral", "blush"}:
                        continue
                    color_check_terms.add(part_lower)
        elif "||" in segment:
            for part in segment.split("||"):
                part_key = part.strip().lower()
                if part_key:
                    color_check_terms.add(part_key)

    if not color_check_terms and attribute_filters["color"]:
        color_check_terms = set(attribute_filters["color"])

    return overall_price_filter, "&".join(clean_segments), color_check_terms, attribute_filters


def normalise_search_keyword(keyword: str) -> str:
    _, clean, _, _ = normalise_keyword(keyword)
    return clean


def resolve_search_keyword(args: dict | None, user_message: str = "") -> str:
    for key in ("keyword", "query", "search", "search_keyword", "keywords"):
        value = (args or {}).get(key)
        if value and str(value).strip():
            return str(value).strip()
    return (user_message or "").strip()
