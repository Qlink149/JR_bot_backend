import json

import httpx

from qlink_chatbot.utils.env_load import (
    default_country_code,
    qlink_gupshup_api_key,
    qlink_gupshup_app_name,
    qlink_gupshup_source,
)
from qlink_chatbot.utils.logger_config import logger

_MAX_BODY_LENGTH = 1024


def _normalize_destination(phone_number: str) -> str:
    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if len(digits) == 10:
        return f"{default_country_code}{digits}"
    return digits


def send_interactive_cta_message(phone_number: str, bot_response: dict):
    """Send an interactive CTA URL button message with an image header."""
    logger.info(
        "Sending interactive CTA message",
        extra={"phone_number": phone_number, "bot_response": bot_response},
    )

    destination = _normalize_destination(phone_number=phone_number)
    url = "https://api.gupshup.io/wa/api/v1/msg"

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "apikey": qlink_gupshup_api_key,
    }

    body_text = (bot_response.get("caption") or "Tap below to continue.")[:_MAX_BODY_LENGTH]
    message_payload = {
        "body": body_text,
        "type": "cta_url",
        "display_text": bot_response.get("button_text", "View Product"),
        "url": bot_response.get("button_url", ""),
    }

    data = {
        "channel": "whatsapp",
        "source": qlink_gupshup_source,
        "destination": destination,
        "message": json.dumps(message_payload),
        "src.name": qlink_gupshup_app_name,
    }

    try:
        response = httpx.post(url, headers=headers, data=data)
        logger.info(
            "Interactive CTA message sent",
            extra={"phone_number": phone_number, "response": response.json()},
        )
        return response.json()
    except Exception as e:
        logger.error(
            "Error sending interactive CTA message",
            extra={"phone_number": phone_number, "error": str(e)},
        )
        raise e
