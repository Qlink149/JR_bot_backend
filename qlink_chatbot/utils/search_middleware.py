"""
Search Middleware — unified product search layer.

Routing logic:
  Complex filters (shape, price range, weight, multi-color) → MongoDB directly
  Simple keyword / single attribute → JR Search API first, MongoDB fallback

Callers (chat agent, REST endpoint) always use SearchFilters + execute().
They never need to know which data source answered.
"""
import re
from dataclasses import dataclass, field

from qlink_chatbot.database.mongo_utils import db
from qlink_chatbot.utils.logger_config import logger

products_collection = db["products"]
product_color_collection = db["product_color"]

# ── Constants ────────────────────────────────────────────────────────────────

COMMON_COLORS = {
    "red", "blue", "green", "yellow", "orange", "purple", "pink", "white",
    "black", "grey", "gray", "brown", "beige", "ivory", "cream", "navy",
    "teal", "turquoise", "gold", "silver", "rust", "terracotta", "coral",
    "indigo", "violet", "maroon", "burgundy", "multi", "multicolor",
    "charcoal", "tan", "olive", "sage", "mustard", "peach", "lavender",
    "copper", "rose", "blush", "aqua", "lime",
}

KNOWN_MATERIALS = {
    "wool", "silk", "viscose", "cotton", "jute", "hemp", "polyester",
    "acrylic", "bamboo", "zari", "linen", "nylon",
}

KNOWN_CONSTRUCTIONS = {
    "hand knotted", "hand tufted", "hand loom", "flat weave", "flat weaves",
    "shag", "handloom", "handknotted", "handtufted",
}

KNOWN_STYLES = {
    "modern", "traditional", "contemporary", "bohemian", "abstract",
    "geometric", "floral", "tribal", "oriental", "transitional", "antique",
    "vintage", "rustic", "minimalist",
}

NON_BLOCKING_DESCRIPTORS = {"solid", "plain"}

KNOWN_SHAPES = {
    "round", "oval", "square", "runner", "rectangular", "hexagonal", "octagonal",
}

CURRENCY_FIELDS = {
    "INR": "INR_MRP", "USD": "USD_MRP", "EUR": "EUR_MRP",
    "GBP": "GBP_MRP", "AUD": "AUD_MRP", "CHF": "CHF_MRP",
    "SGD": "SGD_MRP", "AED": "AED_MRP",
}

DEFAULT_CURRENCY = "INR"


# ── Filter dataclass ─────────────────────────────────────────────────────────

@dataclass
class SearchFilters:
    """Structured filters for a product search request."""
    colors: list[str] = field(default_factory=list)
    shapes: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    constructions: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    generics: list[str] = field(default_factory=list)
    price_filter: dict | None = None   # {"currency": "USD", "amount": 500.0}
    weight_filter: float | None = None
    currency: str = DEFAULT_CURRENCY
    limit: int = 3
    exclude_keys: list[str] = field(default_factory=list)
    exclude_names: list[str] = field(default_factory=list)

    # ── Constructors ─────────────────────────────────────────────────────────

    @classmethod
    def from_keyword(cls, keyword: str, currency: str = DEFAULT_CURRENCY, limit: int = 3) -> "SearchFilters":
        """Parse a &-separated AI keyword string into structured filters."""
        parts = [p.strip() for p in keyword.split("&")]
        colors, shapes, sizes, materials, constructions, styles = [], [], [], [], [], []
        generics = []
        price_filter = None
        weight_filter = None

        for part in parts:
            lower = part.lower().strip()
            if not lower:
                continue

            found_colors, residual = _extract_colors(lower)
            if found_colors:
                colors.extend(found_colors)
                lower = residual
                if not lower:
                    continue

            # Price: "USD 500", "INR 30000"
            pm = re.match(r'^(inr|usd|eur|gbp|aud|chf|sgd|aed)\s+([\d,]+(?:\.\d+)?)$', lower)
            if pm:
                price_filter = {"currency": pm.group(1).upper(), "amount": float(pm.group(2).replace(",", ""))}
                continue

            # Weight: "8kg", "weight 8"
            wm = re.match(r'^(?:weight\s*)?([\d.]+)\s*kg?$', lower)
            if wm:
                weight_filter = float(wm.group(1))
                continue

            # Size: "8x10"
            if re.match(r'^\d+\s*x\s*\d+$', lower):
                sizes.append(lower.replace(" ", ""))
                continue

            if lower in COMMON_COLORS:
                colors.append(lower)
                continue

            if lower in KNOWN_SHAPES:
                shapes.append(lower)
                continue

            if any(c in lower for c in KNOWN_CONSTRUCTIONS):
                constructions.append(lower)
                continue

            if lower in KNOWN_MATERIALS:
                materials.append(lower)
                continue

            if lower in KNOWN_STYLES:
                styles.append(lower)
                continue

            # Check if a shape word is embedded in a phrase like "8 ft round"
            matched_shape = False
            for shape in KNOWN_SHAPES:
                if re.search(rf'\b{re.escape(shape)}\b', lower):
                    shapes.append(shape)
                    lower = re.sub(rf'\b{re.escape(shape)}\b', '', lower).strip()
                    matched_shape = True
                    break

            if lower:
                generics.append(lower)

        return cls(
            colors=list(dict.fromkeys(colors)),
            shapes=list(dict.fromkeys(shapes)),
            sizes=sizes,
            materials=materials,
            constructions=constructions,
            styles=styles,
            generics=generics,
            price_filter=price_filter,
            weight_filter=weight_filter,
            currency=currency,
            limit=limit,
        )

    @classmethod
    def from_params(
        cls,
        colors: list[str] | None = None,
        shapes: list[str] | None = None,
        sizes: list[str] | None = None,
        materials: list[str] | None = None,
        constructions: list[str] | None = None,
        styles: list[str] | None = None,
        generics: list[str] | None = None,
        price_max: float | None = None,
        currency: str = DEFAULT_CURRENCY,
        weight_max: float | None = None,
        limit: int = 3,
        exclude_keys: list[str] | None = None,
        exclude_names: list[str] | None = None,
    ) -> "SearchFilters":
        """Build filters from explicit parameters (used by REST endpoint)."""
        price_filter = None
        price_amount = float(price_max) if price_max is not None else None
        if price_amount is not None and price_amount > 0 and currency:
            price_filter = {"currency": currency.upper(), "amount": price_amount}
        weight_filter = float(weight_max) if weight_max is not None else None
        if weight_filter is not None and weight_filter <= 0:
            weight_filter = None
        return cls(
            colors=[c.lower().strip() for c in (colors or []) if c],
            shapes=[s.lower().strip() for s in (shapes or []) if s],
            sizes=sizes or [],
            materials=[m.lower().strip() for m in (materials or []) if m],
            constructions=constructions or [],
            styles=[
                s.lower().strip()
                for s in (styles or [])
                if s and s.lower().strip() not in NON_BLOCKING_DESCRIPTORS
            ],
            generics=generics or [],
            price_filter=price_filter,
            weight_filter=weight_filter,
            currency=currency.upper() if currency else DEFAULT_CURRENCY,
            limit=limit,
            exclude_keys=[str(k).strip().upper() for k in (exclude_keys or []) if k],
            exclude_names=[_normalize_name(n) for n in (exclude_names or []) if n],
        )

    # ── Routing decision ─────────────────────────────────────────────────────

    def needs_mongodb(self) -> bool:
        """All chat product searches use the synced MongoDB catalog."""
        return True

    def to_jr_keyword(self) -> str:
        """Reconstruct a keyword string for the JR Search API."""
        parts = list(self.colors) + list(self.sizes) + list(self.materials) + \
                list(self.constructions) + list(self.styles) + list(self.generics)
        if self.price_filter:
            parts.append(f"{self.price_filter['currency']} {self.price_filter['amount']}")
        return "&".join(p for p in parts if p)

    def has_any_filter(self) -> bool:
        return any([self.colors, self.shapes, self.sizes, self.materials,
                    self.constructions, self.styles, self.generics,
                    self.price_filter, self.weight_filter])


# ── Main entry point ─────────────────────────────────────────────────────────

async def search(filters: SearchFilters, client_ip: str = "") -> list[dict]:
    """
    Execute a product search using the best available data source.

    Returns a list of formatted product dicts (same shape regardless of source).
    Returns {"error": "..."} dict on no results.
    """
    try:
        # Resolve display currency
        currency = filters.currency if filters.currency in CURRENCY_FIELDS else DEFAULT_CURRENCY
        if currency == DEFAULT_CURRENCY and client_ip:
            currency = await _resolve_currency_from_ip(client_ip)
        currency_field = CURRENCY_FIELDS.get(currency, "INR_MRP")

        # ── Route: MongoDB ────────────────────────────────────────────────────
        logger.info(f"Middleware->MongoDB: colors={filters.colors} shapes={filters.shapes} sizes={filters.sizes} constructions={filters.constructions}")
        return await _mongo_search(filters, currency, currency_field)

    except Exception as e:
        logger.error(f"Middleware search error: {e}")
        return {"error": str(e)}


# ── MongoDB search ────────────────────────────────────────────────────────────

async def _mongo_search(filters: SearchFilters, currency: str, currency_field: str) -> list[dict]:
    fetch_limit = max(filters.limit * 8, 24)
    color_sku_filter: list[str] = []
    color_sku_scores: dict = {}
    query_colors = filters.colors[:]

    if filters.colors:
        color_sku_filter, color_sku_scores = _resolve_color_sku_scores(filters.colors)
        if color_sku_filter:
            query_colors = []

    # Field fallback sequences
    if len(query_colors) == 1:
        color_fields = ["search.color.single", "search.color.multi", None]
    elif len(query_colors) > 1:
        color_fields = ["search.color.multi", None]
    else:
        color_fields = [None]

    size_fields = ["search.size.exact", "search.size.group", None] if filters.sizes else [None]
    material_fields = (
        ["search.material.primary", "search.material.family", "search.material.details", None]
        if filters.materials else [None]
    )

    results = []

    def _try_combos(sku_filter):
        for c_field in color_fields:
            if results:
                break
            for s_field in size_fields:
                if results:
                    break
                for m_field in material_fields:
                    q = _build_query(
                        c_field, query_colors, s_field, filters.sizes,
                        m_field, filters.materials, filters.constructions,
                        filters.styles, filters.price_filter, filters.generics,
                        sku_filter, filters.shapes, filters.exclude_keys,
                    )
                    found = list(products_collection.find(q, {"_id": 0}).sort([("_id", 1)]).limit(fetch_limit))
                    if found:
                        results.extend(found)
                        logger.info(f"MongoDB hit: {len(found)} [c={c_field} s={s_field} m={m_field}]")
                        break

    _try_combos(color_sku_filter)

    if not results and color_sku_filter:
        logger.info("Retrying without color_sku_filter (field-based color)")
        _try_combos([])

    if not results and filters.styles:
        q = _build_query(None, query_colors, None, filters.sizes, None, filters.materials,
                         filters.constructions, [], filters.price_filter,
                         filters.generics + filters.styles, color_sku_filter, filters.shapes,
                         filters.exclude_keys)
        results = list(products_collection.find(q, {"_id": 0}).sort([("_id", 1)]).limit(fetch_limit))
        if not results and color_sku_filter:
            q = _build_query(None, query_colors, None, filters.sizes, None, filters.materials,
                             filters.constructions, [], filters.price_filter,
                             filters.generics + filters.styles, [], filters.shapes,
                             filters.exclude_keys)
            results = list(products_collection.find(q, {"_id": 0}).sort([("_id", 1)]).limit(fetch_limit))

    if not results and (filters.price_filter or filters.weight_filter):
        q = _build_query(None, [], None, [], None, [], [], [], filters.price_filter, [], [],
                         filters.shapes, filters.exclude_keys)
        results = list(products_collection.find(q, {"_id": 0}).sort([("_id", 1)]).limit(fetch_limit))

    if not results and filters.generics and any([
        filters.colors,
        filters.shapes,
        filters.sizes,
        filters.materials,
        filters.constructions,
        filters.styles,
        filters.price_filter,
        filters.weight_filter,
    ]):
        logger.info(f"Retrying MongoDB search without generic terms: {filters.generics}")
        q = _build_query(
            None, query_colors, None, filters.sizes, None, filters.materials,
            filters.constructions, filters.styles, filters.price_filter, [],
            color_sku_filter, filters.shapes, filters.exclude_keys
        )
        results = list(products_collection.find(q, {"_id": 0}).sort([("_id", 1)]).limit(fetch_limit))
        if not results and color_sku_filter:
            q = _build_query(
                None, query_colors, None, filters.sizes, None, filters.materials,
                filters.constructions, filters.styles, filters.price_filter, [],
                [], filters.shapes, filters.exclude_keys
            )
            results = list(products_collection.find(q, {"_id": 0}).sort([("_id", 1)]).limit(fetch_limit))

    if not results and not filters.has_any_filter():
        q = _build_query(None, [], None, [], None, [], [], [], None, [], [], [],
                         filters.exclude_keys)
        results = list(products_collection.find(q, {"_id": 0}).sort([("_id", 1)]).limit(fetch_limit))

    if not results:
        return {"error": "No products found."}

    if filters.exclude_names:
        excluded_names = set(filters.exclude_names)
        results = [
            p for p in results
            if _normalize_name((p.get("raw") or p).get("Name")) not in excluded_names
        ]
        if not results:
            return {"error": "No products found."}

    if filters.weight_filter is not None:
        results = [
            p for p in results
            if _weight_ok(p.get("search", {}).get("weight"), filters.weight_filter)
        ]

    if color_sku_scores:
        results = sorted(
            results,
            key=lambda p: (
                _top_color_pct(color_sku_scores.get(_sku(p), {}).get("colors", {}), filters.colors),
                color_sku_scores.get(_sku(p), {}).get("total_percentage", 0),
            ),
            reverse=True,
        )
        unique = _dedupe_by_sku(results)
        selected = unique[:filters.limit]
    else:
        unique = _dedupe_by_sku(results)
        selected = unique[:filters.limit]

    return _format(selected, currency, currency_field, filters.colors, color_sku_scores)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_colors(text: str) -> tuple[list[str], str]:
    extracted = []
    residual = text
    for color in sorted(COMMON_COLORS, key=len, reverse=True):
        pattern = rf"\b{re.escape(color)}\b"
        if re.search(pattern, residual):
            extracted.append(color)
            residual = re.sub(pattern, " ", residual)
    residual = re.sub(r"\b(and|or|with|in|of|for|rug|rugs)\b", " ", residual)
    residual = re.sub(r"\s+", " ", residual).strip()
    return extracted, residual


def _resolve_color_sku_scores(colors: list[str], limit: int = 80) -> tuple[list[str], dict]:
    if not colors:
        return [], {}
    pattern = "|".join(
        rf"(^|[^a-z]){re.escape(c.lower().strip())}([^a-z]|$)" for c in colors if c
    )
    pipeline = [
        {"$project": {
            "sku": {"$ifNull": ["$SKU", "$sku"]},
            "color_raw": {"$ifNull": ["$PrimaryColorName", {"$ifNull": ["$Primary Color Name",
                         {"$ifNull": ["$ColorName", {"$ifNull": ["$color", "$Colour"]}]}]}]},
            "percentage": {"$ifNull": ["$ColorPercentage",
                          {"$ifNull": ["$Percentage", {"$ifNull": ["$percentage", 0]}]}]},
        }},
        {"$addFields": {
            "sku": {"$toUpper": {"$trim": {"input": {"$toString": {"$ifNull": ["$sku", ""]}}}}},
            "color": {"$toLower": {"$trim": {"input": {"$toString": {"$ifNull": ["$color_raw", ""]}}}}},
        }},
        {"$match": {"color": {"$regex": pattern}}},
        {"$group": {"_id": {"sku": "$sku", "color": "$color"},
                    "percentage": {"$sum": {"$ifNull": ["$percentage", 0]}}}},
        {"$group": {"_id": "$_id.sku", "total_percentage": {"$sum": "$percentage"},
                    "colors": {"$push": {"k": "$_id.color", "v": "$percentage"}}}},
        {"$sort": {"total_percentage": -1}},
        {"$limit": limit},
    ]
    aggregated = list(product_color_collection.aggregate(pipeline))
    ordered, scores = [], {}
    for row in aggregated:
        sku_val = (row.get("_id") or "").strip().upper()
        if not sku_val:
            continue
        color_map = {e["k"]: float(e.get("v", 0) or 0) for e in row.get("colors", []) if e.get("k")}
        if len(colors) > 1 and not all(
            any(_color_match(k, req) for k in color_map) for req in colors
        ):
            continue
        matched = sum(v for k, v in color_map.items() if any(_color_match(k, req) for req in colors))
        if matched <= 0:
            continue
        ordered.append(sku_val)
        scores[sku_val] = {"total_percentage": matched, "colors": color_map}
    return ordered, scores


def _color_match(color_text: str, requested: str) -> bool:
    text = str(color_text or "").lower().strip()
    req = str(requested or "").lower().strip()
    return bool(text and req and re.search(rf"(^|[^a-z]){re.escape(req)}([^a-z]|$)", text))


def _build_query(
    color_field, colors, size_field, sizes, material_field, materials,
    constructions, styles, price_filter, generics, sku_filter, shapes, exclude_keys=None,
) -> dict:
    q: dict = {"flags.inStock": True}
    and_clauses = []

    exclude_keys = [str(k).strip().upper() for k in (exclude_keys or []) if k]
    if exclude_keys:
        and_clauses.append({
            "$and": [
                {"SKU": {"$nin": exclude_keys}},
                {"BarCode": {"$nin": exclude_keys}},
                {"raw.SKU": {"$nin": exclude_keys}},
                {"raw.BarCode": {"$nin": exclude_keys}},
            ]
        })

    if color_field and colors:
        q[color_field] = {"$regex": "|".join(re.escape(c) for c in colors), "$options": "i"}
    if size_field and sizes:
        q[size_field] = {"$regex": "|".join(re.escape(s) for s in sizes), "$options": "i"}
    if material_field and materials:
        q[material_field] = {"$regex": "|".join(re.escape(m) for m in materials), "$options": "i"}
    if constructions:
        q["search.construction"] = {"$regex": "|".join(re.escape(c) for c in constructions), "$options": "i"}
    if styles:
        q["search.style"] = {"$regex": "|".join(re.escape(s) for s in styles), "$options": "i"}
    if shapes:
        q["search.shape"] = {"$regex": "|".join(re.escape(s) for s in shapes), "$options": "i"}
    if price_filter:
        if price_filter["currency"] == "INR":
            q["search.price"] = {"$gt": 0, "$lte": price_filter["amount"]}
        else:
            field = CURRENCY_FIELDS.get(price_filter["currency"])
            if field:
                q[f"raw.{field}"] = {"$gt": 0, "$lte": price_filter["amount"]}
    if sku_filter:
        and_clauses.append({"$or": [
            {"raw.SKU": {"$in": sku_filter}}, {"SKU": {"$in": sku_filter}},
            {"raw.BarCode": {"$in": sku_filter}}, {"BarCode": {"$in": sku_filter}},
        ]})
    if generics:
        regex = "|".join(re.escape(g) for g in generics)
        and_clauses.append({"$or": [
            {"raw.Name": {"$regex": regex, "$options": "i"}},
            {"raw.Collection": {"$regex": regex, "$options": "i"}},
            {"raw.Design": {"$regex": regex, "$options": "i"}},
            {"raw.Quality": {"$regex": regex, "$options": "i"}},
            {"raw.Shape": {"$regex": regex, "$options": "i"}},
        ]})
    if and_clauses:
        q["$and"] = and_clauses
    return q


def _sku(product: dict) -> str:
    raw = product.get("raw", product)
    for key in ("SKU", "BarCode"):
        v = product.get(key) or raw.get(key)
        if v:
            return str(v).strip().upper()
    return ""


def _normalize_name(name) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def _dedupe_by_sku(products: list[dict]) -> list[dict]:
    seen, unique = set(), []
    for p in products:
        key = _sku(p) or str(id(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _top_color_pct(color_map: dict, requested: list[str]) -> float:
    return max(
        (v for k, v in color_map.items() if any(_color_match(k, r) for r in requested)),
        default=0.0,
    )


def _weight_ok(weight, ceiling: float) -> bool:
    try:
        return float(weight) <= ceiling
    except (TypeError, ValueError):
        return True


async def _resolve_currency_from_ip(ip: str) -> str:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            resp = await c.get(f"http://ip-api.com/json/{ip}?fields=currency,status")
            data = resp.json()
            if data.get("status") == "success":
                cur = data.get("currency", "").upper()
                if cur in CURRENCY_FIELDS:
                    return cur
    except Exception:
        pass
    return DEFAULT_CURRENCY


def _format(products: list[dict], currency: str, currency_field: str,
            colors: list[str], color_sku_scores: dict) -> list[dict]:
    out = []
    for p in products:
        raw = p.get("raw", p)
        search = p.get("search", {})
        color = search.get("color", {})
        material = search.get("material", {})
        size = search.get("size", {})
        sku = _sku(p)
        score = color_sku_scores.get(sku, {})
        color_map = score.get("colors", {})
        highest_color = max(color_map, key=lambda k: color_map[k], default="") if color_map else ""
        highest_pct = color_map.get(highest_color, 0.0)
        slug = raw.get("ProductURL") or ""
        barcode = p.get("BarCode") or raw.get("BarCode") or ""
        display_name = raw.get("Name") or raw.get("Design") or raw.get("Collection") or raw.get("GrColor") or sku
        out.append({
            "url": f"https://www.jaipurrugs.com/in/rugs/{slug}?barcode={barcode}" if slug else "",
            "price": {"currency": currency, "amount": raw.get(currency_field)},
            "name": display_name,
            "SKU": sku,
            "barcode": barcode,
            "collection": raw.get("Collection", ""),
            "size": size.get("exact", raw.get("SizeInFT", "")),
            "shape": search.get("shape", raw.get("Shape", "")),
            "color": color.get("single", raw.get("GrColor", "")),
            "color_family": color.get("multi", raw.get("ColorFamily", "")),
            "matched_color_percentage": {
                "total": score.get("total_percentage", 0),
                "by_color": color_map,
                "highest": {"color": highest_color, "percentage": highest_pct},
            },
            "style": search.get("style", raw.get("Style", "")),
            "construction": search.get("construction", raw.get("Construction", "")),
            "material": material.get("primary", raw.get("Material", "")),
            "fabric": material.get("details", raw.get("MaterialDetails", "")),
            "quality": search.get("quality", raw.get("Quality", "")),
            "room": search.get("room", [r.strip() for r in (raw.get("Room") or "").split(",") if r.strip()]),
            "weight": search.get("weight", raw.get("Weight", 0.0)),
            "image": raw.get("HeadShot", ""),
            "mrp": {
                "INR": raw.get("INR_MRP"), "USD": raw.get("USD_MRP"),
                "EUR": raw.get("EUR_MRP"), "GBP": raw.get("GBP_MRP"),
                "AUD": raw.get("AUD_MRP"), "CHF": raw.get("CHF_MRP"),
                "SGD": raw.get("SGD_MRP"), "AED": raw.get("AED_MRP"),
            },
        })
    return out
