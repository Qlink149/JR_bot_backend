import re
import threading
import time

from qlink_chatbot.database.mongo_utils import db
from qlink_chatbot.utils.jr_search_aliases import COLOR_ALIASES
from qlink_chatbot.utils.logger_config import logger

product_color_collection = db["product_color"]
products_collection = db["products"]

# Coarse palette stored in JR.product_color (not GrColor/BrColor labels).
BREAKDOWN_COLORS: frozenset[str] = frozenset({
    "Black", "Blue", "Brown", "Green", "Grey", "Orange",
    "Pink", "Purple", "Red", "White", "Yellow",
})
_BREAKDOWN_COLORS_LOWER = {c.lower(): c for c in BREAKDOWN_COLORS}

_indexes_ensured = False
_backfill_done = False
_instock_skus_by_color: dict[str, frozenset[str]] = {}
_cache_lock = threading.Lock()
_SKU_BATCH = 1000
_BACKFILL_BATCH = 500


def ensure_color_breakdown_indexes() -> None:
    global _indexes_ensured
    if _indexes_ensured:
        return
    try:
        product_color_collection.create_index([("color", 1)])
        product_color_collection.create_index([("SKU", 1)])
        products_collection.create_index([("flags.inStock", 1), ("raw.SKU", 1)])
        products_collection.create_index([("flags.inStock", 1), ("breakdown_colors", 1)])
        _indexes_ensured = True
        logger.info("[MONGO] color breakdown indexes ensured")
    except Exception as err:
        logger.warning(f"[MONGO] failed to ensure color breakdown indexes: {err}")


def breakdown_colors_index_ready() -> bool:
    ensure_color_breakdown_indexes()
    if _backfill_done:
        return True
    try:
        tagged = products_collection.count_documents({"breakdown_colors.0": {"$exists": True}})
        instock = products_collection.count_documents({"flags.inStock": True})
        return instock > 0 and tagged >= int(instock * 0.9)
    except Exception:
        return False


def ensure_breakdown_colors_backfilled() -> None:
    """One-time backfill of products.breakdown_colors from JR.product_color."""
    global _backfill_done
    if _backfill_done:
        return
    if breakdown_colors_index_ready():
        _backfill_done = True
        return

    with _cache_lock:
        if _backfill_done:
            return

        started = time.perf_counter()
        updated_colors = 0
        for color in sorted(BREAKDOWN_COLORS):
            skus = product_color_collection.distinct("SKU", {"color": color})
            for offset in range(0, len(skus), _BACKFILL_BATCH):
                batch = skus[offset:offset + _BACKFILL_BATCH]
                if not batch:
                    continue
                products_collection.update_many(
                    {"raw.SKU": {"$in": batch}},
                    {"$addToSet": {"breakdown_colors": color}},
                )
            updated_colors += 1
            logger.info(f"[COLOR-CACHE] backfilled breakdown_colors for {color} ({len(skus)} SKUs)")

        _backfill_done = True
        logger.info(
            f"[COLOR-CACHE] breakdown_colors backfill complete "
            f"({updated_colors} palette colors) in "
            f"{int((time.perf_counter() - started) * 1000)}ms"
        )


def load_breakdown_colors_for_sku(sku: str) -> list[str]:
    if not sku:
        return []
    rows = product_color_collection.find({"SKU": sku}, {"color": 1, "_id": 0})
    return sorted({
        str(row.get("color") or "").strip()
        for row in rows
        if row.get("color")
    })


def _color_label_matches_term(term: str, color_label: str) -> bool:
    value = (color_label or "").strip()
    if not value or not term:
        return False
    return bool(re.search(rf"\b{re.escape(term)}\b", value, re.IGNORECASE))


def user_terms_to_breakdown_colors(terms: set[str]) -> set[str]:
    """Map user/alias color tokens to catalog breakdown color names."""
    mapped: set[str] = set()
    for term in terms:
        key = (term or "").strip().lower()
        if not key:
            continue
        if key in _BREAKDOWN_COLORS_LOWER:
            mapped.add(_BREAKDOWN_COLORS_LOWER[key])
            continue
        if key in COLOR_ALIASES:
            for part in COLOR_ALIASES[key].split("||"):
                part_key = part.strip().lower()
                if part_key in _BREAKDOWN_COLORS_LOWER:
                    mapped.add(_BREAKDOWN_COLORS_LOWER[part_key])
                else:
                    for bc_lower, bc in _BREAKDOWN_COLORS_LOWER.items():
                        if _color_label_matches_term(part_key, bc):
                            mapped.add(bc)
            continue
        for bc_lower, bc in _BREAKDOWN_COLORS_LOWER.items():
            if _color_label_matches_term(key, bc):
                mapped.add(bc)
    return mapped


def _load_instock_skus_for_color(color: str) -> frozenset[str]:
    ensure_color_breakdown_indexes()
    started = time.perf_counter()
    all_skus = [
        str(doc.get("SKU") or "").strip()
        for doc in product_color_collection.find({"color": color}, {"SKU": 1, "_id": 0})
        if doc.get("SKU")
    ]
    instock: set[str] = set()
    for offset in range(0, len(all_skus), _SKU_BATCH):
        batch = all_skus[offset:offset + _SKU_BATCH]
        cursor = products_collection.find(
            {"flags.inStock": True, "raw.SKU": {"$in": batch}},
            {"raw.SKU": 1, "_id": 0},
        )
        instock.update(
            str(doc["raw"]["SKU"]).strip()
            for doc in cursor
            if doc.get("raw", {}).get("SKU")
        )
    logger.info(
        f"[COLOR-CACHE] {color}: {len(instock)}/{len(all_skus)} in-stock SKUs "
        f"in {int((time.perf_counter() - started) * 1000)}ms"
    )
    return frozenset(instock)


def instock_skus_for_breakdown_colors(breakdown_colors: set[str]) -> set[str]:
    """Cached in-stock SKU set per breakdown palette color."""
    if not breakdown_colors:
        return set()
    result: set[str] = set()
    with _cache_lock:
        for color in sorted(breakdown_colors):
            cached = _instock_skus_by_color.get(color)
            if cached is None:
                cached = _load_instock_skus_for_color(color)
                _instock_skus_by_color[color] = cached
            result.update(cached)
    return result


def skus_matching_breakdown_colors(breakdown_colors: set[str]) -> set[str]:
    return instock_skus_for_breakdown_colors(breakdown_colors)


def load_breakdowns_for_skus(skus: list[str]) -> dict[str, list[dict]]:
    if not skus:
        return {}
    grouped: dict[str, list[dict]] = {}
    cursor = product_color_collection.find(
        {"SKU": {"$in": skus}},
        {"SKU": 1, "color": 1, "percentage": 1, "_id": 0},
    )
    for doc in cursor:
        sku = str(doc.get("SKU") or "").strip()
        if not sku:
            continue
        grouped.setdefault(sku, []).append({
            "color": doc.get("color", ""),
            "percentage": float(doc.get("percentage") or 0),
        })
    for rows in grouped.values():
        rows.sort(key=lambda r: r.get("percentage") or 0, reverse=True)
    return grouped


def product_matches_color_breakdown(
    product: dict,
    breakdown_colors: set[str],
    breakdown_by_sku: dict[str, list[dict]] | None = None,
) -> bool:
    if not breakdown_colors:
        return False
    sku = str(product.get("SKU") or product.get("BarCode") or "").strip()
    if not sku:
        return False
    rows = (breakdown_by_sku or {}).get(sku)
    if rows is None:
        rows = load_breakdowns_for_skus([sku]).get(sku, [])
    if not rows:
        return False
    product_colors = {str(r.get("color") or "") for r in rows}
    return bool(product_colors & breakdown_colors)


def matched_breakdown_for_terms(
    product: dict,
    terms: set[str],
    breakdown_by_sku: dict[str, list[dict]] | None = None,
) -> dict:
    """Build matched_color_percentage payload from product_color rows."""
    sku = str(product.get("SKU") or product.get("BarCode") or "").strip()
    rows = (breakdown_by_sku or {}).get(sku) if sku else None
    if rows is None and sku:
        rows = load_breakdowns_for_skus([sku]).get(sku, [])
    rows = rows or []

    breakdown_colors = user_terms_to_breakdown_colors(terms)
    by_color: dict[str, float] = {}
    for row in rows:
        color = str(row.get("color") or "")
        pct = float(row.get("percentage") or 0)
        if not color:
            continue
        if breakdown_colors and color not in breakdown_colors:
            continue
        by_color[color] = pct

    if not by_color and rows and not breakdown_colors:
        by_color = {
            str(r.get("color") or ""): float(r.get("percentage") or 0)
            for r in rows
            if r.get("color")
        }

    highest = {"color": "", "percentage": 0.0}
    if by_color:
        top_color, top_pct = max(by_color.items(), key=lambda item: item[1])
        highest = {"color": top_color, "percentage": top_pct}

    return {
        "total": round(sum(by_color.values()), 2),
        "by_color": by_color,
        "highest": highest,
        "full_breakdown": [
            {"color": r.get("color", ""), "percentage": float(r.get("percentage") or 0)}
            for r in rows
        ],
    }
