import json

import httpx

from qlink_chatbot.utils.env_load import (
    default_country_code,
    qlink_gupshup_app_id,
    qlink_gupshup_partner_app_token,
    gupshup_product_template_name,
    gupshup_product_template_type,
    qlink_gupshup_app_name,
    qlink_gupshup_source,
)
from qlink_chatbot.utils.logger_config import logger

PRODUCT_TEMPLATE_NAME = gupshup_product_template_name
JAIPURRUGS_BASE_URL = "https://www.jaipurrugs.com/"
PARTNER_BASE_URL = "https://partner.gupshup.io"
_TEMPLATE_ID_CACHE: dict[str, str] = {}


def _normalize_destination(phone_number: str) -> str:
    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if len(digits) == 10:
        return f"{default_country_code}{digits}"
    return digits


def _extract_url_suffix(full_url: str) -> str:
    """Strip the base domain and return the path+query suffix for the dynamic button."""
    if full_url.startswith(JAIPURRUGS_BASE_URL):
        return full_url[len(JAIPURRUGS_BASE_URL):]
    return full_url


def _partner_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": qlink_gupshup_partner_app_token,
        "token": qlink_gupshup_partner_app_token,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _resolve_template_id(template_name: str) -> str:
    """Resolve an elementName to its UUID template id for Partner API sends."""
    if template_name in _TEMPLATE_ID_CACHE:
        return _TEMPLATE_ID_CACHE[template_name]

    response = httpx.get(
        f"{PARTNER_BASE_URL}/partner/app/{qlink_gupshup_app_id}/templates",
        headers=_partner_headers(),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    for template in payload.get("templates", []):
        if template.get("elementName") == template_name:
            template_id = str(template.get("id") or "").strip()
            if template_id:
                _TEMPLATE_ID_CACHE[template_name] = template_id
                return template_id

    return template_name


def send_product_template_message(phone_number: str, bot_response: dict):
    """Send a Gupshup URL-button template message."""
    logger.info(
        "Sending product template message",
        extra={"phone_number": phone_number, "bot_response": bot_response},
    )

    destination = _normalize_destination(phone_number=phone_number)
    url = f"{PARTNER_BASE_URL}/partner/app/{qlink_gupshup_app_id}/template/msg"

    url_suffix = _extract_url_suffix(bot_response.get("button_url", ""))
    template_id = _resolve_template_id(PRODUCT_TEMPLATE_NAME)

    template_payload = {
        "id": template_id,
        "buttons": [
            {
                "index": "0",
                "sub_type": "url",
                "parameters": [{"type": "text", "text": url_suffix}],
            }
        ],
    }

    data = {
        "channel": "whatsapp",
        "source": qlink_gupshup_source,
        "destination": destination,
        "template": json.dumps(template_payload),
        "src.name": qlink_gupshup_app_name,
    }
    if gupshup_product_template_type == "IMAGE" and bot_response.get("image_url"):
        message_payload = {
            "type": "image",
            "originalUrl": bot_response.get("image_url", ""),
            "previewUrl": bot_response.get("image_url", ""),
        }
        data["message"] = json.dumps(message_payload)

    try:
        response = httpx.post(url, headers=_partner_headers(), data=data)
        response.raise_for_status()
        logger.info(
            "Product template message sent",
            extra={"phone_number": phone_number, "response": response.json()},
        )
        return response.json()
    except Exception as e:
        logger.error(
            "Error sending product template message",
            extra={"phone_number": phone_number, "error": str(e)},
        )
        raise e
