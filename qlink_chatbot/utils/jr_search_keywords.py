import re

from qlink_chatbot.utils.jr_search_aliases import (
    COLOR_ALIASES,
    CONSTRUCTION_KEYWORDS,
    MATERIAL_KEYWORDS,
    MULTICOLOR_KEYS,
    NOISE_WORDS,
    PATTERN_ALIASES,
    ROOM_KEYWORDS,
    ROUND_SIZE_PATTERN,
    SHAPE_ALIASES,
    SIZE_PATTERN,
    WEIGHT_PATTERN,
    color_search_terms,
)
from qlink_chatbot.utils.jr_search_currency import extract_price_filter_from_text
from qlink_chatbot.utils.jr_search_sizes import (
    is_cm_dimensions,
    normalise_cm_size_term,
    parse_requested_cm_size,
)


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
    if key in MULTICOLOR_KEYS:
        return "multicolor"
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

    for alias in sorted(MULTICOLOR_KEYS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append("multicolor")
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()
            break

    for match in ROUND_SIZE_PATTERN.finditer(remaining):
        found.append(f"{match.group(1)} round")
        if "round" not in found:
            found.append("round")
    remaining = ROUND_SIZE_PATTERN.sub(" ", remaining).strip()

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
        size_text = f"{m.group(1)}x{m.group(2)}"
        if is_cm_dimensions(int(m.group(1)), int(m.group(2)), remaining):
            found.append(f"{size_text}cm")
        else:
            found.append(size_text)

    if len(found) >= 2:
        return "&".join(found)
    if len(found) == 1:
        if WEIGHT_PATTERN.search(found[0]):
            return found[0].replace(" ", "")
        if found[0].lower() in MULTICOLOR_KEYS:
            return "multicolor"
        if parse_requested_cm_size(found[0]):
            return found[0]
    return keyword.strip()


def track_attribute_terms(segment_key: str, attribute_filters: dict[str, set]) -> None:
    key = segment_key.strip().lower()
    if not key:
        return

    if key in MULTICOLOR_KEYS:
        attribute_filters["multicolor"].add("multicolor")
        return

    if key in COLOR_ALIASES:
        attribute_filters["color"].update(color_search_terms(key))
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
            a, b = int(m.group(1)), int(m.group(2))
            if is_cm_dimensions(a, b, key):
                attribute_filters["size_cm"].add(normalise_cm_size_term(a, b))
            else:
                attribute_filters["size"].add(f"{a}x{b}".lower())
        return

    round_match = ROUND_SIZE_PATTERN.search(key)
    if round_match:
        attribute_filters["size"].add(f"{round_match.group(1)} round".lower())
        attribute_filters["shape"].add("round")
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
        "color": set(), "shape": set(), "size": set(), "size_cm": set(),
        "material": set(), "construction": set(), "pattern": set(),
        "room": set(), "weight_max": set(), "multicolor": set(),
    }

    for segment in keyword.split("&"):
        segment = segment.strip().lower()
        if not segment:
            continue

        segment = ROUND_SIZE_PATTERN.sub(lambda m: f"{m.group(1)} round", segment)
        size_match = SIZE_PATTERN.search(segment)
        if size_match:
            a, b = int(size_match.group(1)), int(size_match.group(2))
            if is_cm_dimensions(a, b, segment):
                attribute_filters["size_cm"].add(normalise_cm_size_term(a, b))
            segment = SIZE_PATTERN.sub(lambda m: f"{m.group(1)}x{m.group(2)}", segment)
        else:
            segment = SIZE_PATTERN.sub(lambda m: f"{m.group(1)}x{m.group(2)}", segment)

        weight_match = WEIGHT_PATTERN.search(segment)
        weight_token = ""
        if weight_match:
            attribute_filters["weight_max"].add(float(weight_match.group(1)))
            weight_token = weight_match.group(0).replace(" ", "").lower()
            segment = (segment[:weight_match.start()] + segment[weight_match.end():]).strip()

        price_filter, residual = extract_price_filter_from_text(segment)
        if price_filter:
            overall_price_filter = price_filter
            segment = residual.strip()

        words = [w for w in segment.split() if w.lower() not in NOISE_WORDS]
        segment = " ".join(words).strip()
        if segment:
            pending_segments.append(segment)
        elif weight_token:
            pending_segments.append(weight_token)

    multi_attribute = len(pending_segments) > 1

    for segment in pending_segments:
        expanded = expand_term(segment, multi_attribute=multi_attribute)
        clean_segments.append(expanded)
        key = segment.strip().lower()
        track_attribute_terms(key, attribute_filters)

        if key in MULTICOLOR_KEYS:
            pass
        elif key in COLOR_ALIASES:
            color_check_terms.update(color_search_terms(key))
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
    model_kw = ""
    for key in ("keyword", "query", "search", "search_keyword", "keywords"):
        value = (args or {}).get(key)
        if value and str(value).strip():
            model_kw = str(value).strip()
            break

    message = (user_message or "").strip()
    if message:
        parsed = preprocess_natural_language(message)
        if "&" in parsed:
            return parsed
        if parsed.strip().lower() != message.strip().lower():
            return parsed

    return model_kw or message
