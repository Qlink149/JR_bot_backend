import httpx

from qlink_chatbot.utils.logger_config import logger

# Country ISO code → currency code
_COUNTRY_CURRENCY: dict[str, str] = {
    "IN": "INR",
    "AE": "AED",
    "US": "USD",
    "GB": "GBP",
    "AU": "AUD",
    "SG": "SGD",
    "CH": "CHF",
    # Eurozone
    "FR": "EUR", "DE": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR",
    "BE": "EUR", "AT": "EUR", "PT": "EUR", "FI": "EUR", "IE": "EUR",
    "GR": "EUR", "LU": "EUR", "SK": "EUR", "SI": "EUR", "EE": "EUR",
    "LV": "EUR", "LT": "EUR", "CY": "EUR", "MT": "EUR",
    # Other common markets
    "SA": "SAR", "QA": "QAR", "KW": "KWD", "BH": "BHD", "OM": "OMR",
    "CA": "CAD", "NZ": "NZD", "JP": "JPY", "CN": "CNY",
    "ZA": "ZAR", "MX": "MXN", "BR": "BRL",
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
