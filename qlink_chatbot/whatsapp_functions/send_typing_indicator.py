import json

import httpx

from qlink_chatbot.utils.env_load import (
    default_country_code,
    qlink_gupshup_api_key,
    qlink_gupshup_app_name,
    qlink_gupshup_source,
)
from qlink_chatbot.utils.logger_config import logger


def _normalize_destination(phone_number: str) -> str:
    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if len(digits) == 10:
        return f"{default_country_code}{digits}"
    return digits


def send_typing_indicator(phone_number: str) -> None:
    """Send a WhatsApp typing indicator via Gupshup so the user sees '...' while the bot processes.

    Fails silently — a failed indicator must never block the actual AI response.
    """
    try:
        destination = _normalize_destination(phone_number)
        url = "https://api.gupshup.io/wa/api/v1/msg"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "apikey": qlink_gupshup_api_key,
        }
        data = {
            "channel": "whatsapp",
            "source": qlink_gupshup_source,
            "destination": destination,
            "message": json.dumps({"type": "notification", "payload": {"type": "typing"}}),
            "src.name": qlink_gupshup_app_name,
        }
        response = httpx.post(url, headers=headers, data=data, timeout=5)
        logger.info(
            "Typing indicator sent",
            extra={"phone_number": phone_number, "status_code": response.status_code},
        )
    except Exception as e:
        logger.warning(
            "Typing indicator failed (non-fatal)",
            extra={"phone_number": phone_number, "error": str(e)},
        )
