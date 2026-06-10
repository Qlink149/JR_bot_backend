import re

import httpx

from qlink_chatbot.utils.logger_config import logger

CURRENCY_FIELDS = {
    "INR": "INR_MRP", "USD": "USD_MRP", "EUR": "EUR_MRP",
    "GBP": "GBP_MRP", "AUD": "AUD_MRP", "CHF": "CHF_MRP",
    "SGD": "SGD_MRP", "AED": "AED_MRP",
}

CURRENCY_ALIASES = {
    "inr": "INR", "rs": "INR", "rupee": "INR", "rupees": "INR",
    "usd": "USD", "dollar": "USD", "dollars": "USD",
    "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
    "aud": "AUD", "chf": "CHF", "sgd": "SGD", "aed": "AED",
}

CALLING_CODE_TO_CURRENCY: dict[str, str] = {
    "91": "INR", "971": "AED", "966": "AED", "965": "AED",
    "974": "AED", "968": "AED", "973": "AED", "61": "AUD",
    "41": "CHF", "423": "CHF", "44": "GBP", "65": "SGD",
    "1": "USD", "49": "EUR", "33": "EUR", "39": "EUR",
    "34": "EUR", "31": "EUR", "32": "EUR", "43": "EUR",
    "351": "EUR", "30": "EUR", "358": "EUR", "353": "EUR",
    "352": "EUR", "356": "EUR", "386": "EUR", "421": "EUR",
    "372": "EUR", "371": "EUR", "370": "EUR", "357": "EUR",
}
DEFAULT_CURRENCY = "INR"

PRICE_OPERATOR_WORDS = {
    "above": "$gte", "over": "$gte", "more than": "$gte",
    "greater than": "$gte", "higher than": "$gte",
    "minimum": "$gte", "min": "$gte", "from": "$gte",
    "starting from": "$gte", "at least": "$gte",
    "below": "$lte", "under": "$lte", "less than": "$lte",
    "lower than": "$lte", "maximum": "$lte", "max": "$lte",
    "up to": "$lte", "upto": "$lte", "within": "$lte",
    "budget": "$lte", "for": "$lte",
}

AMOUNT_MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000,
    "l": 100_000, "lac": 100_000, "lacs": 100_000,
    "lakh": 100_000, "lakhs": 100_000, "lc": 100_000,
    "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000,
    "m": 1_000_000, "million": 1_000_000,
}

PRICE_CONTEXT_WORDS = {
    "price", "priced", "cost", "costing", "amount", "budget", "range",
    "mrp", "rs", "rupee", "rupees", "inr", "usd", "eur", "gbp", "aud",
    "chf", "sgd", "aed", "dollar", "dollars", "euro", "euros", "pound", "pounds",
}


def normalize_currency_code(value: str) -> str:
    normalized = (value or DEFAULT_CURRENCY).lower().strip()
    return CURRENCY_ALIASES.get(normalized, normalized.upper())


def format_price_amount(value) -> str:
    if value is None or value == "":
        return ""
    try:
        amount = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return str(value).strip()
    if amount.is_integer():
        return f"{int(amount):,}"
    return f"{amount:,.2f}".rstrip("0").rstrip(".")


def build_display_price(currency: str, amount) -> str:
    formatted = format_price_amount(amount)
    return f"{currency} {formatted}" if formatted else ""


def _currency_alias_pattern() -> str:
    return "|".join(
        re.escape(a) for a in sorted(CURRENCY_ALIASES, key=len, reverse=True)
    )


def extract_requested_currency_from_text(text: str) -> str:
    lower = (text or "").lower()
    alias_pattern = _currency_alias_pattern()
    for pattern in [
        rf"\bin\s*({alias_pattern})\b",
        rf"\bin({alias_pattern})\b",
        rf"\b({alias_pattern})\s*(?:price|prices|pricing|mrp)\b",
        rf"\b(?:price|prices|pricing|mrp)\s*(?:in\s*)?({alias_pattern})\b",
    ]:
        m = re.search(pattern, lower)
        if m:
            c = normalize_currency_code(m.group(1))
            if c in CURRENCY_FIELDS:
                return c
    return ""


def resolve_currency_from_country_code(country_code: str) -> str:
    digits = re.sub(r"\D", "", country_code or "")
    for length in range(min(3, len(digits)), 0, -1):
        c = CALLING_CODE_TO_CURRENCY.get(digits[:length])
        if c:
            return c
    return ""


async def resolve_currency_from_ip(ip: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"http://ip-api.com/json/{ip}?fields=currency,status")
            data = resp.json()
            if data.get("status") == "success":
                c = data.get("currency", "").upper()
                if c in CURRENCY_FIELDS:
                    logger.info(f"[CURRENCY] resolved {c} from IP {ip}")
                    return c
    except Exception as e:
        logger.warning(f"[CURRENCY] IP geolocation failed for {ip}: {e}")
    return DEFAULT_CURRENCY


def _parse_amount_with_suffix(amount_text: str, suffix: str = "") -> float:
    amount = float(amount_text.replace(",", ""))
    return amount * AMOUNT_MULTIPLIERS.get((suffix or "").lower(), 1)


def _is_probable_price_amount(amount: float, suffix: str, context: str) -> bool:
    if suffix:
        return True
    if amount >= 1000:
        return True
    return any(w in context.split() for w in PRICE_CONTEXT_WORDS)


def _infer_thousand_range(min_a: float, max_a: float) -> tuple[float, float]:
    """Treat shorthand like '20 to 30,000' as 20k–30k for common budget phrasing."""
    low, high = min(min_a, max_a), max(min_a, max_a)
    if low < 1000 and high >= 1000 and low * 1000 <= high * 1.5:
        return low * 1000, high
    return low, high


def price_filter_to_keyword(price_filter: dict) -> str:
    currency = normalize_currency_code(price_filter.get("currency", DEFAULT_CURRENCY))
    if "min_amount" in price_filter and "max_amount" in price_filter:
        min_a = int(price_filter["min_amount"])
        max_a = int(price_filter["max_amount"])
        return f"between {currency} {min_a} to {currency} {max_a}"
    amount = int(price_filter["amount"])
    operator = price_filter.get("operator", "$lte")
    if operator == "$gte":
        return f"above {currency} {amount}"
    return f"under {currency} {amount}"


def extract_price_filter_from_text(text: str) -> tuple[dict | None, str]:
    """Return (price_filter, cleaned_text_without_price)."""
    alias_pattern = _currency_alias_pattern()
    operators = "|".join(
        re.escape(w) for w in sorted(PRICE_OPERATOR_WORDS, key=len, reverse=True)
    )
    amount_core = r"([\d,]+(?:\.\d+)?)"
    amount_suffix = r"(?:(?<![a-z])(?:k|thousand|lacs?|lakhs?|lakh|lc|cr|crores?|m|million)(?![a-z]))?"
    amount = rf"{amount_core}\s*{amount_suffix}"

    range_match = re.search(
        rf"\bbetween\s+(?:({alias_pattern})\s*)?{amount}\s+"
        rf"(?:and|to|-)\s+(?:({alias_pattern})\s*)?{amount}",
        text,
    )
    if range_match:
        fc, min_t, sc, max_t = range_match.groups()
        min_a, max_a = _infer_thousand_range(
            _parse_amount_with_suffix(min_t, ""),
            _parse_amount_with_suffix(max_t, ""),
        )
        currency = fc or sc or DEFAULT_CURRENCY
        return (
            {
                "currency": normalize_currency_code(currency),
                "min_amount": min_a,
                "max_amount": max_a,
            },
            (text[:range_match.start()] + " " + text[range_match.end():]).strip(),
        )

    plain_range = re.search(
        rf"(?:\b(?:around|about|approximately)\s+)?{amount}\s+(?:and|to|-)\s+{amount}\b",
        text,
    )
    if plain_range:
        min_t, max_t = plain_range.groups()
        min_a, max_a = _infer_thousand_range(
            _parse_amount_with_suffix(min_t, ""),
            _parse_amount_with_suffix(max_t, ""),
        )
        if _is_probable_price_amount(min_a, "", text) or _is_probable_price_amount(max_a, "", text):
            return (
                {
                    "currency": DEFAULT_CURRENCY,
                    "min_amount": min_a,
                    "max_amount": max_a,
                },
                (text[:plain_range.start()] + " " + text[plain_range.end():]).strip(),
            )

    patterns = [
        rf"\b({operators})\b\s+({alias_pattern})\s+{amount_core}{amount_suffix}\b",
        rf"\b({operators})\b\s*{amount}\s*({alias_pattern})\b",
        rf"\b({operators})\b\s*(?:({alias_pattern})\s*)?{amount}",
        rf"\b(?:({alias_pattern})\s*)?{amount}\s*\b({operators})\b",
        rf"\b({alias_pattern})\s+{amount_core}{amount_suffix}\b",
        rf"\b{amount_core}{amount_suffix}\s*({alias_pattern})\b",
        rf"\b{amount_core}{amount_suffix}\b",
    ]
    def _unpack_price_match(idx: int, groups: tuple) -> tuple[str, str, str, str]:
        """Map regex groups to operator, currency, amount, suffix."""
        n = len(groups)
        if idx == 0:
            return (
                groups[0] if n > 0 else "",
                groups[1] if n > 1 else "",
                groups[2] if n > 2 else "",
                groups[3] if n > 3 else "",
            )
        if idx == 1:
            return (
                groups[0] if n > 0 else "",
                groups[2] if n > 2 else "",
                groups[1] if n > 1 else "",
                groups[3] if n > 3 else "",
            )
        if idx == 2:
            return (
                groups[0] if n > 0 else "",
                groups[1] if n > 1 else "",
                groups[2] if n > 2 else "",
                groups[3] if n > 3 else "",
            )
        if idx == 3:
            return (
                groups[2] if n > 2 else "budget",
                groups[0] if n > 0 else "",
                groups[1] if n > 1 else "",
                groups[3] if n > 3 else "",
            )
        if idx == 4:
            return ("budget", groups[0] if n > 0 else "", groups[1] if n > 1 else "", "")
        if idx == 5:
            return ("budget", groups[1] if n > 1 else "", groups[0] if n > 0 else "", "")
        return ("budget", DEFAULT_CURRENCY, groups[0] if n > 0 else "", groups[1] if n > 1 else "")

    for idx, pattern in enumerate(patterns):
        m = re.search(pattern, text)
        if not m:
            continue
        groups = m.groups()
        op, cur, at, sf = _unpack_price_match(idx, groups)

        parsed = _parse_amount_with_suffix(at, sf or "")
        if not cur and not _is_probable_price_amount(
            parsed, sf or "", f"{text[:m.start()]} {text[m.end():]}"
        ):
            continue

        return (
            {
                "currency": normalize_currency_code(cur or DEFAULT_CURRENCY),
                "amount": parsed,
                "operator": PRICE_OPERATOR_WORDS.get(op, "$lte"),
            },
            (text[:m.start()] + " " + text[m.end():]).strip(),
        )

    return None, text


def apply_price_filter(products: list, price_filter: dict) -> list:
    if not price_filter:
        return products
    currency = price_filter.get("currency", "INR")
    currency_field = CURRENCY_FIELDS.get(currency, "INR_MRP")
    result = []
    for p in products:
        raw_price = p.get(currency_field)
        try:
            price = float(str(raw_price).replace(",", "")) if raw_price not in (None, "", "0") else 0.0
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        if "min_amount" in price_filter and price < price_filter["min_amount"]:
            continue
        if "max_amount" in price_filter and price > price_filter["max_amount"]:
            continue
        if "amount" in price_filter:
            amount = price_filter["amount"]
            op = price_filter.get("operator", "$lte")
            if op == "$lte" and price > amount:
                continue
            if op == "$gte" and price < amount:
                continue
            if op == "$lt" and price >= amount:
                continue
            if op == "$gt" and price <= amount:
                continue
        result.append(p)
    return result
