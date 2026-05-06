import json

import httpx

from qlink_chatbot.constants import GUPSHUP_SOURCE
from qlink_chatbot.utils.env_load import (
    default_country_code,
    gupshup_api_key,
    gupshup_app_name,
    gupshup_source,
)
from qlink_chatbot.utils.logger_config import logger


def _normalize_destination(phone_number: str) -> str:
    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if len(digits) == 10:
        return f"{default_country_code}{digits}"
    return digits


def send_image_message(phone_number: str, bot_response: dict):
    """Sends an image message with caption to a phone number."""
    logger.info(
        "Sending image message to phone number",
        extra={"phone_number": phone_number, "bot_response": bot_response},
    )

    destination = _normalize_destination(phone_number=phone_number)
    url = "https://api.gupshup.io/wa/api/v1/msg"

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "apikey": gupshup_api_key,
    }

    # Create image message payload
    message_payload = {
        "type": "image",
        "originalUrl": bot_response.get("image_url"),
        "previewUrl": bot_response.get("image_url"),
        "caption": bot_response.get("caption", ""),
    }

    data = {
        "channel": "whatsapp",
        "source": gupshup_source or GUPSHUP_SOURCE,
        "destination": destination,
        "message": json.dumps(message_payload),
        "src.name": gupshup_app_name,
    }

    try:
        response = httpx.post(url, headers=headers, data=data)
        logger.info(
            "Image message sent successfully",
            extra={
                "phone_number": phone_number,
                "response": response.json(),
            },
        )
        return response.json()
    except Exception as e:
        logger.error(
            "Error in sending image message",
            extra={"phone_number": phone_number, "error": str(e)},
        )
        raise e
