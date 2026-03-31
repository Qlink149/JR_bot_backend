from qlink_chatbot.utils.logger_config import logger
from qlink_chatbot.whatsapp_functions.media.send_image import send_image_message
from qlink_chatbot.whatsapp_functions.send_text_message import send_text_message


def _normalize_responses(bot_responses):
    if bot_responses is None:
        return []
    if isinstance(bot_responses, list):
        return bot_responses
    return [bot_responses]


def dispatch_whatsapp_responses(phone_number: str, bot_responses):
    """Send one or multiple bot responses to WhatsApp using Gupshup helpers."""
    responses = _normalize_responses(bot_responses)

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

        if response_type == "image":
            send_image_message(phone_number=phone_number, bot_response=response)
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
