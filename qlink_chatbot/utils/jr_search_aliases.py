import re

SIZE_PATTERN = re.compile(r"\b(\d+)\s*(?:x|by|\*|X)\s*(\d+)\b", re.IGNORECASE)
ROUND_SIZE_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s*['′]?\s*round\b", re.IGNORECASE)
WEIGHT_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s*kg\b", re.IGNORECASE)
MULTICOLOR_KEYS = frozenset({"multicolor", "multi", "multicolour", "colorful", "multi color"})
NOISE_WORDS = {
    "show", "me", "find", "search", "looking", "look", "need", "want",
    "please", "rug", "rugs", "carpet", "carpets", "in", "the", "a", "an",
    "and", "or", "with", "of", "for",
}

COLOR_ALIASES: dict[str, str] = {
    "red": "Red||Crimson||Rust||Scarlet||Maroon",
    "crimson": "Crimson||Red||Scarlet",
    "rust": "Rust||Copper||Copper Tan||Terracotta",
    "terracotta": "Terracotta||Rust||Copper||Burnt Orange",
    "orange": "Orange||Copper||Rust||Amber",
    "copper": "Copper||Copper Tan||Rust",
    "maroon": "Maroon||Crimson||Red",
    "burgundy": "Burgundy||Crimson||Maroon",
    "blue": "Blue||Navy||Teal",
    "navy": "Navy||Blue||Indigo",
    "navy blue": "Navy||Blue",
    "teal": "Teal||Blue||Turquoise",
    "indigo": "Indigo||Navy||Blue",
    "green": "Green||Olive||Sage||Jade",
    "olive": "Green||Olive||Sage||Moss",
    "sage": "Green||Sage||Olive||Mint",
    "emerald": "Green||Emerald||Jade",
    "grey": "Classic Gray||Charcoal||Slate||Gray",
    "gray": "Classic Gray||Charcoal||Slate||Gray",
    "charcoal": "Charcoal||Classic Gray||Slate",
    "silver": "Classic Gray||Silver||Gray",
    "black": "Black||Charcoal||Ebony",
    "dark": "Charcoal||Dark||Black||Navy Blue",
    "white": "White||Ivory||Antique White||Cream",
    "ivory": "Ivory||Antique White||Cream||White",
    "cream": "Cream||Ivory||Antique White||White",
    "off-white": "Ivory||Antique White||Cream||White",
    "off white": "Ivory||Antique White||Cream||White",
    "beige": "Beige||Sand||Camel||Tan",
    "sand": "Sand||Beige||Camel||Tan",
    "tan": "Tan||Sand||Camel||Copper Tan",
    "taupe": "Taupe||Beige||Sand",
    "brown": "Brown||Chocolate||Walnut||Caramel",
    "chocolate": "Chocolate||Brown||Walnut",
    "gold": "Gold||Golden||Mustard||Amber",
    "golden": "Golden||Gold||Amber",
    "mustard": "Mustard||Gold||Yellow",
    "yellow": "Yellow||Mustard||Gold",
    "pink": "Pink||Blush||Rose||Mauve||Coral",
    "blush": "Blush||Pink||Rose",
    "rose": "Rose||Pink||Blush",
    "coral": "Coral||Terracotta||Rust",
    "mauve": "Mauve||Blush||Pink",
    "purple": "Purple||Lavender||Violet||Plum||Wisteria||Amethyst",
    "lavender": "Lavender||Purple||Lilac||Wisteria",
    "violet": "Violet||Purple||Lavender",
    "wisteria": "Wisteria||Lavender||Purple",
}

SHAPE_ALIASES: dict[str, str] = {
    "round": "Round",
    "circular": "Round",
    "circle": "Round",
    "oval": "Oval",
    "square": "Square",
    "runner": "Runner",
    "rectangular": "Rectangle",
    "rectangle": "Rectangle",
    "irregular": "Irregular",
}

PATTERN_ALIASES: dict[str, str] = {
    "solid": "Solid",
    "plain": "Solid",
    "geometric": "Geometric",
    "geo": "Geometric",
    "floral": "Floral",
    "abstract": "Abstract",
    "tribal": "Moroccan and Tribal",
    "moroccan": "Moroccan and Tribal",
    "boho": "Moroccan and Tribal",
    "bohemian": "Moroccan and Tribal",
    "medallion": "Medallion",
    "modern": "Modern",
    "contemporary": "Contemporary",
    "traditional": "Traditional",
    "classic": "Traditional",
    "transitional": "Transitional",
    "vintage": "Traditional",
}

MATERIAL_KEYWORDS = (
    "wool and bamboo silk", "wool and viscose", "bamboo silk", "pure silk",
    "wool", "silk", "viscose", "cotton", "bamboo", "jute", "leather", "nylon",
)
CONSTRUCTION_KEYWORDS = (
    "hand knotted", "hand tufted", "hand loom", "hand woven", "flat weave",
    "machine made", "handmade",
)
ROOM_KEYWORDS = (
    "living room", "dining room", "bedroom", "outdoor", "bathroom",
    "kitchen", "hallway", "office", "kids room", "entryway",
)

API_SEARCH_FIELDS = (
    "ProductType", "Name", "Collection", "Design", "SKU", "BarCode", "ProductURL",
    "GrColor", "BrColor", "ColorFamily", "DisplayFilter", "ColorMood", "BasicColor",
    "Style", "StylePattern", "Pattern", "DecoreStyle", "Designer",
    "SizeInFT", "SizeGroupInFT", "SizeInCM", "SizeGroupInCM",
    "Material", "MaterialDetails", "MaterialFamilies",
    "Construction", "Quality", "Texture", "Shape",
    "Room", "MultiFilter", "FullDescription", "ShortDescription",
    "PileThickness", "ProductTag",
)

MONGO_FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "color": (
        "raw.GrColor", "raw.BrColor", "raw.ColorFamily",
        "raw.DisplayFilter", "raw.ColorMood", "raw.BasicColor",
    ),
    "pattern": ("raw.Pattern", "raw.Style", "raw.StylePattern", "raw.DecoreStyle"),
    "size": ("raw.SizeInFT", "raw.SizeInCM"),
    "material": ("raw.Material", "raw.MaterialDetails", "raw.MaterialFamilies"),
    "construction": ("raw.Construction",),
    "shape": ("raw.Shape",),
    "room": ("raw.Room", "raw.MultiFilter"),
    "general": tuple(f"raw.{f}" for f in API_SEARCH_FIELDS),
}


def collect_known_catalog_values() -> tuple[set[str], set[str]]:
    colors: set[str] = set()
    patterns: set[str] = set()
    for key, expansion in COLOR_ALIASES.items():
        colors.add(key.lower())
        for part in expansion.split("||"):
            colors.add(part.strip().lower())
    for key, value in PATTERN_ALIASES.items():
        patterns.add(key.lower())
        patterns.add(value.lower())
    return colors, patterns


KNOWN_COLOR_VALUES, KNOWN_PATTERN_VALUES = collect_known_catalog_values()
KNOWN_SHAPE_VALUES = {k.lower() for k in SHAPE_ALIASES} | {v.lower() for v in SHAPE_ALIASES.values()}
