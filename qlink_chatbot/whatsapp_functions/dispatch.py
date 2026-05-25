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

IMAGE_TO_CTA_DELAY_SECONDS = float(
    os.getenv("WHATSAPP_IMAGE_CTA_DELAY_SECONDS", "2.5")
)


def _normalize_responses(bot_responses):
    if bot_responses is None:
        return []
    if isinstance(bot_responses, list):
        return bot_responses
    return [bot_responses]


def dispatch_whatsapp_responses(phone_number: str, bot_responses):
    """Send one or multiple bot responses to WhatsApp using Gupshup helpers."""
    responses = _normalize_responses(bot_responses)
    previous_response_type = None

    for response in responses:
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

        if (
            response_type == "interactive_cta"
            and previous_response_type == "image"
            and IMAGE_TO_CTA_DELAY_SECONDS > 0
        ):
            logger.info(
                "Waiting before CTA send so image media renders first",
                extra={
                    "phone_number": phone_number,
                    "delay_seconds": IMAGE_TO_CTA_DELAY_SECONDS,
                },
            )
            time.sleep(IMAGE_TO_CTA_DELAY_SECONDS)

        if response_type == "image":
            send_image_message(phone_number=phone_number, bot_response=response)
            previous_response_type = response_type
            continue

        if response_type == "interactive_cta":
            send_interactive_cta_message(
                phone_number=phone_number, bot_response=response
            )
            previous_response_type = response_type
            continue

        if response_type == "product_template":
            send_product_template_message(
                phone_number=phone_number, bot_response=response
            )
            previous_response_type = response_type
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
            previous_response_type = response_type
            continue

        logger.warning(
            "Unsupported whatsapp response type",
            extra={"response_type": response_type, "phone_number": phone_number},
        )
        previous_response_type = response_type
