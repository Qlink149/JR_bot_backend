import asyncio

import httpx

from qlink_chatbot.utils.env_load import (
    qlink_gupshup_app_id,
    qlink_gupshup_partner_app_token,
)
from qlink_chatbot.utils.logger_config import logger

PARTNER_BASE_URL = "https://partner.gupshup.io"


async def send_typing_indicator(message_id: str) -> None:
    """Mark an inbound WhatsApp message as read and show typing via Gupshup.

    This must use the Partner API with the inbound message id. Sending a
    notification payload through the normal message API appears as visible text.
    """
    if not message_id or not qlink_gupshup_app_id or not qlink_gupshup_partner_app_token:
        logger.info(
            "Skipping WhatsApp typing indicator; missing message id or Gupshup partner config",
            extra={"message_id_present": bool(message_id)},
        )
        return

    url = f"{PARTNER_BASE_URL}/partner/app/{qlink_gupshup_app_id}/v1/event"
    headers = {
        "Authorization": qlink_gupshup_partner_app_token,
        "token": qlink_gupshup_partner_app_token,
        "Content-Type": "application/json",
    }
    payload = {
        "type": "message-event",
        "message": {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        },
    }

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            logger.warning(
                "WhatsApp typing indicator failed",
                extra={
                    "status_code": response.status_code,
                    "response": response.text[:500],
                },
            )
            return
        logger.info("WhatsApp typing indicator sent", extra={"status_code": response.status_code})
    except Exception as e:
        logger.warning("WhatsApp typing indicator failed", extra={"error": str(e)})


async def typing_indicator_loop(message_id: str, stop_event: asyncio.Event) -> None:
    """Send one typing indicator; WhatsApp hides it on reply or after about 25s."""
    await send_typing_indicator(message_id)
    await stop_event.wait()
