import json

import httpx

from qlink_chatbot.database.mongo_utils import whatsapp_outbound_events_collection
from qlink_chatbot.utils.env_load import (
    default_country_code,
    qlink_gupshup_app_id,
    qlink_gupshup_partner_app_token,
)
from qlink_chatbot.utils.logger_config import logger

PARTNER_BASE_URL = "https://partner.gupshup.io"


def _normalize_destination(phone_number: str) -> str:
    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if len(digits) == 10:
        return f"{default_country_code}{digits}"
    return digits


def _save_outbound_event(phone_number: str, response_type: str, status: str, details: dict):
    try:
        whatsapp_outbound_events_collection.insert_one(
            {
                "phone_number": phone_number,
                "response_type": response_type,
                "status": status,
                "details": details,
            }
        )
    except Exception as e:
        logger.warning("Failed to persist outbound event", extra={"error": str(e)})


def send_image_message(phone_number: str, bot_response: dict):
    """Sends an image message with caption to a phone number."""
    logger.info(
        "Sending image message to phone number",
        extra={"phone_number": phone_number, "bot_response": bot_response},
    )

    destination = _normalize_destination(phone_number=phone_number)
    url = f"{PARTNER_BASE_URL}/partner/app/{qlink_gupshup_app_id}/v3/message"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": qlink_gupshup_partner_app_token,
        "token": qlink_gupshup_partner_app_token,
    }

    image_payload = {"link": bot_response.get("image_url")}
    if bot_response.get("caption"):
        image_payload["caption"] = bot_response.get("caption", "")

    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": destination,
        "type": "image",
        "image": json.dumps(image_payload),
    }

    try:
        response = httpx.post(url, headers=headers, data=data)
        response.raise_for_status()
        response_payload = response.json()
        _save_outbound_event(
            phone_number,
            "image",
            "submitted",
            {
                "status_code": response.status_code,
                "response": response_payload,
                "destination": destination,
            },
        )
        logger.info(
            "Image message sent successfully",
            extra={
                "phone_number": phone_number,
                "response": response_payload,
            },
        )
        return response_payload
    except Exception as e:
        response = getattr(e, "response", None)
        _save_outbound_event(
            phone_number,
            "image",
            "error",
            {
                "error": str(e),
                "status_code": getattr(response, "status_code", None),
                "response": getattr(response, "text", "")[:1000] if response else "",
                "destination": destination,
            },
        )
        logger.error(
            "Error in sending image message",
            extra={"phone_number": phone_number, "error": str(e)},
        )
        raise e
