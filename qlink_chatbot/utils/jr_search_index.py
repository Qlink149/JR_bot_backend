import re

from qlink_chatbot.database.mongo_utils import db
from qlink_chatbot.utils.jr_search_aliases import SHAPE_ALIASES, SIZE_PATTERN
from qlink_chatbot.utils.jr_search_sizes import parse_cm_field
from qlink_chatbot.utils.logger_config import logger

products_collection = db["products"]

_indexes_ensured = False

_TOKEN_SOURCE_FIELDS = (
    "GrColor", "BrColor", "ColorFamily", "DisplayFilter", "ColorMood", "BasicColor",
    "Material", "MaterialDetails", "MaterialFamilies",
    "SizeInFT", "SizeInCM", "SizeGroupInFT", "SizeGroupInCM",
    "Construction", "Pattern", "Style", "StylePattern", "DecoreStyle",
    "Shape", "Room", "MultiFilter", "Quality", "Name", "Collection",
    "Designer", "Design", "Texture", "ProductTag",
)

# Drop composition / filler scraps from MaterialDetails etc. (e.g. "30%", "yarn", "and").
_TOKEN_NOISE = frozenset({
    "and", "or", "the", "of", "with", "for", "a", "an", "in", "to", "by",
    "yarn", "yarns", "pct", "percent", "percentage",
})
_PERCENT_TOKEN = re.compile(r"^\d+%$")
_FEET_SIZE_PATTERN = re.compile(
    r"\b(\d+)\s*['′]?\s*[xX*by]\s*(\d+)\s*['′]?",
    re.IGNORECASE,
)

_COLOR_TOKEN_FIELDS = ("GrColor", "BrColor", "DisplayFilter", "ColorMood", "BasicColor")
_NON_COLOR_TOKEN_FIELDS = tuple(
    f for f in _TOKEN_SOURCE_FIELDS if f not in _COLOR_TOKEN_FIELDS and f != "ColorFamily"
)


def _normalize_size_text(text: str) -> str:
    """Collapse size phrases to NxM before splitting into tokens."""
    # "8x11'2" / "8X11'6" → "8x11" before other patterns touch the trailing inches
    text = re.sub(
        r"(\d+)\s*[xX*]\s*(\d+)\s*['′]\s*\d+",
        lambda m: f"{m.group(1)}x{m.group(2)}",
        text,
    )
    text = SIZE_PATTERN.sub(lambda m: f"{m.group(1)}x{m.group(2)}", text)
    # "5' x 8'" / "5′ x 8′"
    text = _FEET_SIZE_PATTERN.sub(lambda m: f"{m.group(1)}x{m.group(2)}", text)
    return text


def _is_useful_token(part: str) -> bool:
    if not part or part in _TOKEN_NOISE:
        return False
    if _PERCENT_TOKEN.match(part):
        return False
    # Keep single-digit sizes only when part of NxM (handled separately); drop bare "2" from 8x11'2
    if part.isdigit() and len(part) == 1:
        return False
    return len(part) >= 2 or part.isdigit()


def build_search_tokens(product: dict) -> list[str]:
    """Lowercase search tokens indexed on each product document.

    Built from Product Master fields (API does not return tokens). Used by Mongo
    `$all` / `$in` clauses for construction, material, size, shape, etc.
    """
    tokens: set[str] = set()

    def add(value) -> None:
        if value is None or value == "":
            return
        text = _normalize_size_text(str(value).lower())
        for part in re.split(r"[\s,/|&\-+()\[\]'\"]+", text):
            part = part.strip(".")
            if _is_useful_token(part):
                tokens.add(part)

    for field in _COLOR_TOKEN_FIELDS:
        add(product.get(field))

    display_filter = (product.get("DisplayFilter") or "").lower()
    if re.search(r"\b(multi|multicolor|multicolour)\b", display_filter):
        tokens.update({"multi", "multicolor", "multicolour"})

    # Site ColorFamily chips (incl. "Red and Orange") — index label + each
    # constituent so primary-color recall matches website filters. Ranking
    # keeps pure GrColor above soft family hits.
    color_family = (product.get("ColorFamily") or "").strip()
    if color_family:
        if color_family.lower() == "multi":
            tokens.update({"multi", "multicolor", "multicolour"})
        else:
            add(color_family)
            for part in re.split(r"\s+and\s+", color_family, flags=re.IGNORECASE):
                part = part.strip()
                if part:
                    add(part)

    for field in _NON_COLOR_TOKEN_FIELDS:
        add(product.get(field))

    cm_dims = parse_cm_field(str(product.get("SizeInCM") or ""))
    if cm_dims:
        tokens.add(f"{cm_dims[0]}x{cm_dims[1]}")

    shape = (product.get("Shape") or "").strip()
    if shape:
        tokens.add(shape.lower())
        tokens.add(SHAPE_ALIASES.get(shape.lower(), shape).lower())

    # Merchandising facets from ProductTag / BestSellerStatus / Quality.
    product_tag = str(product.get("ProductTag") or "").strip().lower()
    if product_tag:
        tokens.add(product_tag)
        if product_tag == "new":
            tokens.update({"new", "newarrival", "arrival"})
        elif "bestseller" in product_tag or "best seller" in product_tag:
            tokens.add("bestseller")
        elif product_tag == "outdoor":
            tokens.add("outdoor")
    if product.get("BestSellerStatus") is True:
        tokens.add("bestseller")
    quality = str(product.get("Quality") or "").strip().lower()
    if "antique" in quality:
        tokens.add("antique")
    size_group = str(product.get("SizeGroupInFT") or "").strip().lower()
    if "swatch" in size_group:
        tokens.update({"swatch", "swatches"})
    if "oversize" in size_group:
        tokens.add("oversize")
    # Normalize SizeGroup chips to NxM / Ndiaround tokens for recall.
    dia_match = re.search(r"(\d+(?:\.\d+)?)\s*dia(?:meter)?\s*round", size_group)
    if dia_match:
        tokens.add(f"{dia_match.group(1)}diaround")
        tokens.add(f"{dia_match.group(1)} round")
    size_match = SIZE_PATTERN.search(size_group.replace(" ", ""))
    if size_match:
        tokens.add(f"{size_match.group(1)}x{size_match.group(2)}")

    # SKU / barcode for exact lookups (Lorenzo-style designer already via Designer field)
    for id_field in ("SKU", "BarCode", "Design"):
        raw_id = str(product.get(id_field) or "").strip().lower()
        if not raw_id:
            continue
        tokens.add(raw_id)
        for part in re.split(r"[^a-z0-9]+", raw_id):
            if _is_useful_token(part):
                tokens.add(part)

    weight = product.get("Weight")
    if weight not in (None, "", 0, 0.0):
        try:
            kg = float(weight)
            if kg > 0:
                tokens.add(f"{int(kg) if kg.is_integer() else kg}kg")
        except (TypeError, ValueError):
            pass

    return sorted(tokens)


def backfill_search_tokens(
    batch_size: int = 500,
    *,
    rebuild_all: bool = False,
    after_id=None,
) -> dict:
    """Add or rebuild search_tokens on product docs.

    Default: only docs missing tokens. Set rebuild_all=True after tokenizer changes.
    Pass after_id (ObjectId) to paginate a full rebuild.
    """
    ensure_product_search_indexes()
    updated = 0
    last_id = after_id
    query: dict = {"raw": {"$exists": True}}
    if after_id is not None:
        query["_id"] = {"$gt": after_id}
    if not rebuild_all:
        query["$or"] = [
            {"search_tokens": {"$exists": False}},
            {"search_tokens": None},
            {"search_tokens": {"$size": 0}},
        ]
    cursor = products_collection.find(
        query,
        {"raw": 1},
        max_time_ms=60_000,
    ).sort("_id", 1).limit(batch_size)
    for doc in cursor:
        last_id = doc["_id"]
        raw = doc.get("raw")
        if not raw:
            continue
        tokens = build_search_tokens(raw)
        products_collection.update_one(
            {"_id": doc["_id"]},
            {"$set": {"search_tokens": tokens}},
        )
        updated += 1
    remaining = products_collection.count_documents(
        {
            "raw": {"$exists": True},
            "$or": [
                {"search_tokens": {"$exists": False}},
                {"search_tokens": None},
                {"search_tokens": {"$size": 0}},
            ],
        }
    )
    return {
        "updated": updated,
        "remaining_without_tokens": remaining,
        "rebuild_all": rebuild_all,
        "last_id": str(last_id) if last_id is not None else None,
    }


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
