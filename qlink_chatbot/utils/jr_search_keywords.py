import re

from qlink_chatbot.utils.jr_search_aliases import (
    CATALOG_TAG_ALIASES,
    COLOR_ALIASES,
    HINGLISH_COLOR_ALIASES,
    CONSTRUCTION_KEYWORDS,
    DIA_ROUND_PATTERN,
    MATERIAL_KEYWORDS,
    MULTICOLOR_KEYS,
    NOISE_WORDS,
    PATTERN_ALIASES,
    ROOM_KEYWORDS,
    ROUND_SIZE_PATTERN,
    SHAPE_ALIASES,
    SIZE_CATEGORIES,
    SIZE_CATEGORY_ALIASES,
    SIZE_PATTERN,
    WEIGHT_PATTERN,
    color_search_terms,
    normalise_catalog_tag,
    normalise_size_category,
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

    for alias in sorted(CATALOG_TAG_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append(CATALOG_TAG_ALIASES[alias])
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()

    for kw in sorted(CONSTRUCTION_KEYWORDS, key=len, reverse=True):
        if kw in remaining:
            found.append(kw)
            remaining = remaining.replace(kw, " ").strip()

    for kw in sorted(MATERIAL_KEYWORDS, key=len, reverse=True):
        if kw in remaining:
            found.append(kw)
            remaining = remaining.replace(kw, " ").strip()

    for alias in sorted(HINGLISH_COLOR_ALIASES, key=len, reverse=True):
        # Devanagari has no \b word boundaries — use plain contains for non-ASCII.
        if alias.isascii():
            pattern = rf"\b{re.escape(alias)}\b"
        else:
            pattern = re.escape(alias)
        if re.search(pattern, remaining):
            found.append(HINGLISH_COLOR_ALIASES[alias])
            remaining = re.sub(pattern, " ", remaining).strip()

    for alias in sorted(COLOR_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append(alias)
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()

    for alias in sorted(MULTICOLOR_KEYS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append("multicolor")
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()
            break

    for match in DIA_ROUND_PATTERN.finditer(remaining):
        found.append(f"{match.group(1)} dia round")
        if "round" not in found:
            found.append("round")
    remaining = DIA_ROUND_PATTERN.sub(" ", remaining).strip()

    for match in ROUND_SIZE_PATTERN.finditer(remaining):
        found.append(f"{match.group(1)} round")
        if "round" not in found:
            found.append("round")
    remaining = ROUND_SIZE_PATTERN.sub(" ", remaining).strip()

    for alias in sorted(SHAPE_ALIASES, key=len, reverse=True):
        # Devanagari (गोल) has no \b word boundaries — use plain contains.
        if alias.isascii():
            pattern = rf"\b{re.escape(alias)}\b"
        else:
            pattern = re.escape(alias)
        if re.search(pattern, remaining):
            found.append(alias)
            remaining = re.sub(pattern, " ", remaining).strip()

    for alias in sorted(PATTERN_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append(alias)
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()

    for room in sorted(ROOM_KEYWORDS, key=len, reverse=True):
        if room in remaining:
            found.append(room)
            remaining = remaining.replace(room, " ").strip()

    for alias in sorted(SIZE_CATEGORY_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", remaining):
            found.append(SIZE_CATEGORY_ALIASES[alias])
            remaining = re.sub(rf"\b{re.escape(alias)}\b", " ", remaining).strip()

    for category in sorted(SIZE_CATEGORIES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(category)}\b", remaining):
            found.append(category)
            remaining = re.sub(rf"\b{re.escape(category)}\b", " ", remaining).strip()

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

    if found:
        # Single hits matter for soft phrasing ("is there any new arrival" → new).
        ordered = list(dict.fromkeys(found))
        if len(ordered) == 1 and WEIGHT_PATTERN.search(ordered[0]):
            joined = ordered[0].replace(" ", "")
        else:
            joined = "&".join(ordered)
        # Keep budget phrases that alias extraction removed from `remaining`
        # (e.g. "bestsellers under 1 lakh" → bestseller&under INR 100000).
        price_filter, _ = extract_price_filter_from_text(text)
        if price_filter:
            from qlink_chatbot.utils.jr_search_currency import price_filter_to_keyword

            price_kw = price_filter_to_keyword(price_filter)
            if price_kw and price_kw.lower() not in joined.lower():
                return f"{joined}&{price_kw}"
        return joined
    return keyword.strip()


def track_attribute_terms(segment_key: str, attribute_filters: dict[str, set]) -> None:
    key = segment_key.strip().lower()
    if not key:
        return

    tag = normalise_catalog_tag(key)
    if tag:
        attribute_filters["catalog_tag"].add(tag)
        return

    if key in MULTICOLOR_KEYS:
        attribute_filters["multicolor"].add("multicolor")
        return

    if key in COLOR_ALIASES:
        attribute_filters["color"].update(color_search_terms(key))
        attribute_filters["color_exact"].add(key)
        return

    if key in SHAPE_ALIASES:
        attribute_filters["shape"].add(SHAPE_ALIASES[key].lower())
        attribute_filters["shape"].add(key)
        return

    if key in PATTERN_ALIASES:
        attribute_filters["pattern"].add(PATTERN_ALIASES[key].lower())
        attribute_filters["pattern"].add(key)
        return

    cat = normalise_size_category(key)
    if cat:
        attribute_filters["size_category"].add(cat)
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

    dia_match = DIA_ROUND_PATTERN.search(key)
    if dia_match:
        attribute_filters["size"].add(f"{dia_match.group(1)} dia round".lower())
        attribute_filters["shape"].add("round")
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
    exact_color_terms: set[str] = set()
    attribute_filters: dict[str, set] = {
        "color": set(), "color_exact": set(), "shape": set(), "size": set(), "size_cm": set(),
        "size_category": set(),
        "material": set(), "construction": set(), "pattern": set(),
        "room": set(), "weight_max": set(), "multicolor": set(),
        "catalog_tag": set(),
    }

    for segment in keyword.split("&"):
        segment = segment.strip().lower()
        if not segment:
            continue

        segment = DIA_ROUND_PATTERN.sub(lambda m: f"{m.group(1)} dia round", segment)
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
        key = segment.strip().lower()
        track_attribute_terms(key, attribute_filters)
        if normalise_size_category(key):
            continue
        tag = normalise_catalog_tag(key)
        if tag:
            # Keep canonical tag token in clean_keyword for Mongo recall.
            clean_segments.append(tag)
            continue

        expanded = expand_term(segment, multi_attribute=multi_attribute)
        clean_segments.append(expanded)

        if key in MULTICOLOR_KEYS:
            pass
        elif key in COLOR_ALIASES:
            color_check_terms.update(color_search_terms(key))
            exact_color_terms.add(key)
        elif "||" in segment:
            for part in segment.split("||"):
                part_key = part.strip().lower()
                if part_key:
                    color_check_terms.add(part_key)
                    exact_color_terms.add(part_key)

    if not color_check_terms and attribute_filters["color"]:
        color_check_terms = set(attribute_filters["color"])
    if not exact_color_terms and attribute_filters["color_exact"]:
        exact_color_terms = set(attribute_filters["color_exact"])

    attribute_filters["color_exact"] = exact_color_terms

    # Hallway/entryway → also search Runner shape (Room field has no Hallway value).
    hallway_rooms = {"hallway", "entryway", "corridor", "passage"}
    rooms = attribute_filters.get("room") or set()
    if rooms & hallway_rooms:
        shapes = attribute_filters.setdefault("shape", set())
        shape_l = {str(s).lower() for s in shapes}
        if "runner" not in shape_l:
            shapes.add("runner")
        runner_token = SHAPE_ALIASES.get("runner", "Runner")
        if not any(str(s).lower() == "runner" for s in clean_segments):
            clean_segments.append(runner_token)

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

    # Tool keyword is authoritative — it includes price/size segments NL preprocess drops.
    if model_kw:
        return model_kw

    message = (user_message or "").strip()
    if message:
        parsed = preprocess_natural_language(message)
        if "&" in parsed:
            return parsed
        if parsed.strip().lower() != message.strip().lower():
            return parsed
        return message

    return ""
