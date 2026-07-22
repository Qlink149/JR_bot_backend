import os
import time

from qlink_chatbot.utils.logger_config import logger
from qlink_chatbot.whatsapp_functions.media.send_image import send_image_message
from qlink_chatbot.whatsapp_functions.media.send_interactive_message import (
    send_interactive_cta_message,
)
from qlink_chatbot.whatsapp_functions.media.send_template_message import (
    send_product_template_message,
)
from qlink_chatbot.whatsapp_functions.send_text_message import send_text_message

# Gap between outbound WA messages (Kisna uses ~0.4s; keep configurable).
OUTBOUND_GAP_SECONDS = float(
    os.getenv("WHATSAPP_OUTBOUND_GAP_SECONDS", "0.45")
)
# Legacy name kept for compatibility with existing deploys.
IMAGE_TO_CTA_DELAY_SECONDS = float(
    os.getenv("WHATSAPP_IMAGE_CTA_DELAY_SECONDS", str(OUTBOUND_GAP_SECONDS))
)


def _normalize_responses(bot_responses):
    if bot_responses is None:
        return []
    if isinstance(bot_responses, list):
        return bot_responses
    return [bot_responses]


def _gap_seconds() -> float:
    # Prefer explicit outbound gap; fall back to legacy env.
    if os.getenv("WHATSAPP_OUTBOUND_GAP_SECONDS"):
        return OUTBOUND_GAP_SECONDS
    return IMAGE_TO_CTA_DELAY_SECONDS


def dispatch_whatsapp_responses(phone_number: str, bot_responses) -> int:
    """Send one or multiple bot responses to WhatsApp using Gupshup helpers.

    Returns the number of items that failed to send.
    """
    responses = _normalize_responses(bot_responses)
    gap = max(0.0, _gap_seconds())
    dispatch_errors = 0

    for index, response in enumerate(responses):
        if index > 0 and gap:
            time.sleep(gap)

        try:
            if isinstance(response, str):
                send_text_message(
                    phone_number=phone_number,
                    bot_response={"type": "text", "text": response},
                )
                continue

            if not isinstance(response, dict):
                logger.warning(
                    "Unsupported response payload type",
                    extra={"response": response, "phone_number": phone_number},
                )
                continue

            response_type = response.get("type", "text")

            if response_type == "image":
                send_image_message(phone_number=phone_number, bot_response=response)
                continue

            if response_type == "interactive_cta":
                send_interactive_cta_message(
                    phone_number=phone_number, bot_response=response
                )
                continue

            if response_type == "product_template":
                send_product_template_message(
                    phone_number=phone_number, bot_response=response
                )
                continue

            if response_type in {"text", "text_with_image"}:
                text = response.get("text") or response.get("caption") or ""
                if text:
                    send_text_message(
                        phone_number=phone_number,
                        bot_response={"type": "text", "text": text},
                    )

                if response_type == "text_with_image" and response.get("image_url"):
                    send_image_message(phone_number=phone_number, bot_response=response)
                continue

            logger.warning(
                "Unsupported whatsapp response type",
                extra={"response_type": response_type, "phone_number": phone_number},
            )
        except Exception as exc:
            # Don't abort the carousel if one card fails (Kisna isolation).
            dispatch_errors += 1
            logger.error(
                "WhatsApp dispatch item failed — continuing",
                extra={
                    "phone_number": phone_number,
                    "index": index,
                    "response": response if isinstance(response, dict) else str(response)[:200],
                    "error": str(exc),
                },
            )

    return dispatch_errors
