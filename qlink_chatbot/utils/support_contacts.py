"""Customer-facing contact numbers by market."""

from __future__ import annotations

import re

INDIA_GENERAL = "+91 8000295928"
INTERNATIONAL_GENERAL = "+91 7412 060 022"
AFTER_SALES_EMAIL = "order-update@jaipurrugs.com"
AFTER_SALES_PHONE = "+91 7665017083"
RUGCARE_EMAIL = "rugcare@jaipurrugs.com"
RUGCARE_PHONE = "+91 9039195506"
SHOP_EMAIL = "shop@jaipurrugs.com"

# Calling code → ISO-ish country hint used for contact + currency routing
CALLING_CODE_TO_COUNTRY: dict[str, str] = {
    "1": "US",
    "44": "GB",
    "61": "AU",
    "65": "SG",
    "971": "AE",
    "41": "CH",
    "49": "DE",
    "33": "FR",
    "91": "IN",
}

_INDIA_COUNTRIES = frozenset({"IN", "IND", "INDIA"})


def country_from_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    for code in sorted(CALLING_CODE_TO_COUNTRY.keys(), key=len, reverse=True):
        if digits.startswith(code):
            return CALLING_CODE_TO_COUNTRY[code]
    return ""


def is_india_market(country_code: str = "", phone: str = "") -> bool:
    cc = (country_code or "").strip().upper()
    if cc in _INDIA_COUNTRIES:
        return True
    if cc and cc not in _INDIA_COUNTRIES:
        return False
    inferred = country_from_phone(phone)
    if inferred:
        return inferred == "IN"
    # Default India when unknown (legacy domestic chatbot behavior)
    return True


def general_support_phone(country_code: str = "", phone: str = "") -> str:
    return INDIA_GENERAL if is_india_market(country_code, phone) else INTERNATIONAL_GENERAL


def support_contacts_blurb(country_code: str = "", phone: str = "") -> str:
    general = general_support_phone(country_code, phone)
    market = "India" if is_india_market(country_code, phone) else "International"
    return (
        f"STRICT CONTACT RULE — use ONLY these customer-service contacts "
        f"(not store showroom numbers). Detected market: {market}. "
        f"General for this customer: {general} (WhatsApp available). "
        f"Shop email: {SHOP_EMAIL}. "
        f"After-sales / tracking: {AFTER_SALES_EMAIL}, {AFTER_SALES_PHONE}. "
        f"Repair / care / washing / services: {RUGCARE_EMAIL}, {RUGCARE_PHONE}. "
        f"Do NOT share the India general number with international customers "
        f"(or the international number with India customers) unless they ask for both."
    )
