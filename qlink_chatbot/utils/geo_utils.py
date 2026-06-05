import re

import httpx

from qlink_chatbot.utils.logger_config import logger

# Country ISO code → currency code (only currencies supported by JR pricing: INR, AED, USD, GBP, AUD, SGD, CHF, EUR)
_COUNTRY_CURRENCY: dict[str, str] = {
    "IN": "INR",
    # GCC — all mapped to AED (JR's supported Gulf currency)
    "AE": "AED", "SA": "AED", "QA": "AED", "KW": "AED", "BH": "AED", "OM": "AED",
    "US": "USD", "CA": "USD",  # North America
    "GB": "GBP",
    "AU": "AUD", "NZ": "AUD",  # Oceania
    "SG": "SGD",
    "CH": "CHF",
    # Eurozone
    "FR": "EUR", "DE": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR",
    "BE": "EUR", "AT": "EUR", "PT": "EUR", "FI": "EUR", "IE": "EUR",
    "GR": "EUR", "LU": "EUR", "SK": "EUR", "SI": "EUR", "EE": "EUR",
    "LV": "EUR", "LT": "EUR", "CY": "EUR", "MT": "EUR",
    # Other markets — default to USD (closest supported international currency)
    "JP": "USD", "CN": "USD", "ZA": "USD", "MX": "USD", "BR": "USD",
}

_PRIVATE_PREFIXES = ("10.", "172.16.", "172.17.", "172.18.", "172.19.",
                     "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                     "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                     "172.30.", "172.31.", "192.168.", "127.", "::1")


def _is_private(ip: str) -> bool:
    return any(ip.startswith(p) for p in _PRIVATE_PREFIXES)


async def get_geo(ip: str) -> dict:
    """Resolve IP address to geo data.

    Returns dict with keys: country_code, country, city, currency.
    Falls back to empty strings on any error or private IP.
    """
    if not ip or _is_private(ip):
        return {"country_code": "", "country": "", "city": "", "currency": ""}

    try:
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,countryCode,regionName,city"},
            )
        data = resp.json()
        if data.get("status") != "success":
            return {"country_code": "", "country": "", "city": "", "currency": ""}

        country_code = data.get("countryCode", "")
        return {
            "country_code": country_code,
            "country":      data.get("country", ""),
            "city":         data.get("city", ""),
            "currency":     _COUNTRY_CURRENCY.get(country_code, "INR"),
        }
    except Exception as e:
        logger.warning("Geo lookup failed", extra={"ip": ip, "error": str(e)})
        return {"country_code": "", "country": "", "city": "", "currency": ""}


def currency_for_country(country_code: str) -> str:
    return _COUNTRY_CURRENCY.get((country_code or "").upper(), "INR")


# E.164 calling code (no +) → ISO country code
_CALLING_CODE_TO_ISO: dict[str, str] = {
    "91": "IN",
    "971": "AE", "966": "SA", "965": "KW", "974": "QA", "968": "OM", "973": "BH",
    "61": "AU", "64": "NZ",
    "41": "CH", "423": "LI",
    "44": "GB",
    "65": "SG",
    "1": "US",
    "49": "DE", "33": "FR", "39": "IT", "34": "ES", "31": "NL", "32": "BE",
    "43": "AT", "351": "PT", "30": "GR", "358": "FI", "353": "IE", "352": "LU",
    "356": "MT", "386": "SI", "421": "SK", "372": "EE", "371": "LV", "370": "LT",
    "357": "CY",
}

# Calling code → display currency (matches JR product MRP fields)
_CALLING_CODE_TO_CURRENCY: dict[str, str] = {
    "91": "INR",
    "971": "AED", "966": "AED", "965": "AED", "974": "AED", "968": "AED", "973": "AED",
    "61": "AUD", "64": "AUD",
    "41": "CHF", "423": "CHF",
    "44": "GBP",
    "65": "SGD",
    "1": "USD",
    "49": "EUR", "33": "EUR", "39": "EUR", "34": "EUR", "31": "EUR", "32": "EUR",
    "43": "EUR", "351": "EUR", "30": "EUR", "358": "EUR", "353": "EUR", "352": "EUR",
    "356": "EUR", "386": "EUR", "421": "EUR", "372": "EUR", "371": "EUR", "370": "EUR",
    "357": "EUR",
}


def parse_whatsapp_phone(phone: str) -> dict:
    """Derive country and currency from a WhatsApp phone number (E.164 without +).

    Returns dict with keys: calling_code, country_iso, currency.
    """
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return {"calling_code": "", "country_iso": "", "currency": "INR"}

    for code in sorted(_CALLING_CODE_TO_ISO, key=len, reverse=True):
        if digits.startswith(code):
            country_iso = _CALLING_CODE_TO_ISO[code]
            return {
                "calling_code": code,
                "country_iso": country_iso,
                "currency": _CALLING_CODE_TO_CURRENCY.get(code, currency_for_country(country_iso)),
            }

    return {"calling_code": "", "country_iso": "", "currency": "INR"}
