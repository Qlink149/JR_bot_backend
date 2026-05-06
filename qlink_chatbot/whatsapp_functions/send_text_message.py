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


def send_text_message(phone_number: str, bot_response: str):
    """Sends a text message to a phone number."""
    logger.info(
        "Sending text message to phone number with message",
        extra={"phone_number": phone_number, "bot_response": bot_response},
    )
    
    source = gupshup_source or GUPSHUP_SOURCE
    app_name = gupshup_app_name

    destination = _normalize_destination(phone_number=phone_number)
    url = "https://api.gupshup.io/wa/api/v1/msg"

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "apikey": gupshup_api_key,
    }

    # Modify the data to match the cURL request format
    data = {
        "channel": "whatsapp",
        "source": source,
        "destination": destination,
        "message": json.dumps(bot_response),
        "src.name": app_name,
    }

    logger.info(
        "Resolved gupshup routing",
        extra={
            "source": source,
            "destination": destination,
            "app_name": app_name,
        },
    )

    try:
        response = httpx.post(url, headers=headers, data=data)
        logger.info(
            "Response",
            extra={
                "phone_number": phone_number,
                "response": response.json(),
            },
        )
    except Exception as e:
        logger.error(
            "Error in sending text message",
            extra={"phone_number": phone_number, "error": e},
        )
        raise e
