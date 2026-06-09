import re

from qlink_chatbot.database.mongo_utils import db
from qlink_chatbot.utils.jr_search_aliases import SHAPE_ALIASES, SIZE_PATTERN
from qlink_chatbot.utils.logger_config import logger

products_collection = db["products"]

_indexes_ensured = False

_TOKEN_SOURCE_FIELDS = (
    "GrColor", "BrColor", "ColorFamily", "DisplayFilter", "ColorMood", "BasicColor",
    "Material", "MaterialDetails", "MaterialFamilies",
    "SizeInFT", "SizeInCM", "SizeGroupInFT",
    "Construction", "Pattern", "Style", "StylePattern", "DecoreStyle",
    "Shape", "Room", "MultiFilter", "Quality", "Name", "Collection",
)


def build_search_tokens(product: dict) -> list[str]:
    """Lowercase search tokens indexed on each product document."""
    tokens: set[str] = set()

    def add(value) -> None:
        if value is None or value == "":
            return
        text = str(value).lower()
        text = SIZE_PATTERN.sub(lambda m: f"{m.group(1)}x{m.group(2)}", text)
        for part in re.split(r"[\s,/|&\-+()\[\]'\"]+", text):
            part = part.strip(".")
            if len(part) >= 2 or (part.isdigit() and part):
                tokens.add(part)

    for field in _TOKEN_SOURCE_FIELDS:
        add(product.get(field))

    shape = (product.get("Shape") or "").strip()
    if shape:
        tokens.add(shape.lower())
        tokens.add(SHAPE_ALIASES.get(shape.lower(), shape).lower())

    weight = product.get("Weight")
    if weight not in (None, "", 0, 0.0):
        try:
            kg = float(weight)
            if kg > 0:
                tokens.add(f"{int(kg) if kg.is_integer() else kg}kg")
        except (TypeError, ValueError):
            pass

    return sorted(tokens)


def backfill_search_tokens(batch_size: int = 500) -> dict:
    """Add search_tokens to existing product docs (run once after deploy)."""
    ensure_product_search_indexes()
    updated = 0
    cursor = products_collection.find(
        {"search_tokens": {"$exists": False}, "raw": {"$exists": True}},
        {"raw": 1},
        max_time_ms=20_000,
    ).limit(batch_size)
    for doc in cursor:
        raw = doc.get("raw")
        if not raw:
            continue
        products_collection.update_one(
            {"_id": doc["_id"]},
            {"$set": {"search_tokens": build_search_tokens(raw)}},
        )
        updated += 1
    remaining = products_collection.count_documents(
        {"search_tokens": {"$exists": False}, "raw": {"$exists": True}}
    )
    return {"updated": updated, "remaining_without_tokens": remaining}


def ensure_product_search_indexes() -> None:
    global _indexes_ensured
    if _indexes_ensured:
        return
    try:
        products_collection.create_index([("flags.inStock", 1)])
        products_collection.create_index([("flags.inStock", 1), ("search_tokens", 1)])
        products_collection.create_index("BarCode", unique=True, sparse=True)
        _indexes_ensured = True
        logger.info("[MONGO] product search indexes ensured")
    except Exception as err:
        logger.warning(f"[MONGO] failed to ensure product indexes: {err}")
