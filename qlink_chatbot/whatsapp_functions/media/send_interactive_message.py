import json

import httpx

from qlink_chatbot.database.mongo_utils import whatsapp_outbound_events_collection
from qlink_chatbot.utils.env_load import (
    default_country_code,
    qlink_gupshup_app_id,
    qlink_gupshup_partner_app_token,
)
from qlink_chatbot.utils.logger_config import logger

_MAX_BODY_LENGTH = 1024
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


def send_interactive_cta_message(phone_number: str, bot_response: dict):
    """Send an interactive CTA URL button message, optionally with an image header.

    If bot_response contains image_url, the image is sent as the message header
    so image and button arrive as one atomic message (no ordering race condition).
    """
    logger.info(
        "Sending interactive CTA message",
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

    body_text = (bot_response.get("caption") or "Tap below to continue.")[:_MAX_BODY_LENGTH]
    interactive_payload = {
        "type": "cta_url",
        "body": {"text": body_text},
        "action": {
            "name": "cta_url",
            "parameters": {
                "display_text": bot_response.get("button_text", "View Product"),
                "url": bot_response.get("button_url", ""),
            },
        },
    }

    # Attach image as header if provided — keeps image + button in one message
    image_url = bot_response.get("image_url")
    if image_url:
        interactive_payload["header"] = {
            "type": "image",
            "image": {"link": image_url},
        }

    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": destination,
        "type": "interactive",
        "interactive": json.dumps(interactive_payload),
    }

    try:
        response = httpx.post(url, headers=headers, data=data)
        response.raise_for_status()
        response_payload = response.json()
        _save_outbound_event(
            phone_number,
            "interactive_cta",
            "submitted",
            {
                "status_code": response.status_code,
                "response": response_payload,
                "destination": destination,
            },
        )
        logger.info(
            "Interactive CTA message sent",
            extra={"phone_number": phone_number, "response": response_payload},
        )
        return response_payload
    except Exception as e:
        response = getattr(e, "response", None)
        _save_outbound_event(
            phone_number,
            "interactive_cta",
            "error",
            {
                "error": str(e),
                "status_code": getattr(response, "status_code", None),
                "response": getattr(response, "text", "")[:1000] if response else "",
                "destination": destination,
            },
        )
        logger.error(
            "Error sending interactive CTA message",
            extra={"phone_number": phone_number, "error": str(e)},
        )
        raise e
