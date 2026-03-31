from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from qlink_chatbot.agent.chat_agent import chat_agent
from qlink_chatbot.database.mongo_utils import (
    create_session,
    get_session_by_id,
    save_message,
    save_user_name,
)
from qlink_chatbot.utils.logger_config import logger
from qlink_chatbot.whatsapp_functions.dispatch import dispatch_whatsapp_responses

whatsapp_router = APIRouter()
WHATSAPP_COLLECTION_NAME = "users_whatsapp"


def _extract_event(request_data: dict) -> dict:
    entry = request_data.get("entry", [])
    changes = entry[0].get("changes", []) if entry else []
    return changes[0].get("value", {}) if changes else {}


def _extract_username(whatsapp_event: dict) -> str:
    contacts = whatsapp_event.get("contacts", [])
    if not contacts:
        return ""
    return contacts[0].get("profile", {}).get("name", "")


def _extract_user_message_text(message_payload: dict) -> str:
    text_body = message_payload.get("text", {}).get("body", "")
    if text_body:
        return text_body.strip()

    button_text = message_payload.get("button", {}).get("text", "")
    if button_text:
        return button_text.strip()

    interactive = message_payload.get("interactive", {})
    if interactive.get("type") == "button_reply":
        return interactive.get("button_reply", {}).get("title", "").strip()

    if interactive.get("type") == "list_reply":
        return interactive.get("list_reply", {}).get("title", "").strip()

    return ""


@whatsapp_router.post("/gupshup/fsai/message")
@whatsapp_router.post("/gupshup/fsai/message")
async def messages(data: Request):
    """Message endpoint to receive and send messages on WhatsApp chatbot."""
    request_data = await data.json()
    logger.info("Request received with data", extra={"data": request_data})

    phone_number = ""
    payload_data = {}

    try:
        if "payload" in request_data:
            logger.info("Payload found in request data, ignoring it")
            return {"status": "ignored", "reason": "payload_callback"}

        whatsapp_event = _extract_event(request_data)

        if "statuses" in whatsapp_event:
            status_payload = whatsapp_event["statuses"][0]
            status = status_payload.get("type") or status_payload.get("status")
            logger.info("Ignoring message with status", extra={"status": status})
            return {"status": "success"}

        incoming_messages = whatsapp_event.get("messages", [])
        if not incoming_messages:
            logger.info("No incoming messages in webhook payload")
            return {"status": "ignored", "reason": "no_messages"}

        message_payload = incoming_messages[0]
        phone_number = message_payload.get("from", "")

        if not phone_number:
            return JSONResponse(
                content={"status": "error", "message": "Missing sender number"},
                status_code=400,
            )

        whatsapp_username = _extract_username(whatsapp_event)
        payload_data = {
            "phone_number": phone_number,
            "messages": message_payload,
            "whatsapp_username": whatsapp_username,
        }
        logger.info(
            "Data object to processor",
            extra={"data": payload_data, "phone_number": phone_number},
        )

        user_text = _extract_user_message_text(message_payload)
        if not user_text:
            logger.info(
                "Ignoring unsupported inbound message type",
                extra={"phone_number": phone_number, "message": message_payload},
            )
            return {"status": "ignored", "reason": "unsupported_message_type"}

        session_id = phone_number.lower()
        session = get_session_by_id(
            session_id=session_id,
            collection_name=WHATSAPP_COLLECTION_NAME,
        )

        if not session:
            create_session(
                session_id=session_id,
                country_code="",
                name=whatsapp_username,
                is_ai=True,
                collection_name=WHATSAPP_COLLECTION_NAME,
            )
            session = {"chat_history": [], "country_code": ""}
        elif whatsapp_username and whatsapp_username != session.get("user_name", ""):
            save_user_name(
                session_id=session_id,
                name=whatsapp_username,
                collection_name=WHATSAPP_COLLECTION_NAME,
            )

        save_message(
            session_id=session_id,
            role="user",
            content=user_text,
            collection_name=WHATSAPP_COLLECTION_NAME,
        )

        bot_text = await chat_agent(
            chat_history=session.get("chat_history", []),
            user_message=user_text,
            session_id=session_id,
            country_code=session.get("country_code", ""),
            client_ip="",
            collection_name=WHATSAPP_COLLECTION_NAME,
        )

        bot_text = bot_text or "Sorry, I could not generate a response right now."
        save_message(
            session_id=session_id,
            role="assistant",
            content=bot_text,
            collection_name=WHATSAPP_COLLECTION_NAME,
        )

        payload_data["bot_response"] = [{"type": "text", "text": bot_text}]
        logger.info(
            "Bot response",
            extra={
                "bot_response": payload_data["bot_response"],
                "phone_number": phone_number,
            },
        )

        dispatch_whatsapp_responses(
            phone_number=phone_number,
            bot_responses=payload_data["bot_response"],
        )

        return {"status": "success"}

    except Exception as e:
        logger.exception(
            "Exception occured while running message endpoint",
            extra={"exception": str(e), "phone_number": phone_number},
        )

        if phone_number:
            try:
                fallback_response = [{"type": "text", "text": "Unexpected error occured."}]
                dispatch_whatsapp_responses(
                    phone_number=phone_number,
                    bot_responses=fallback_response,
                )
            except Exception as send_error:
                logger.error(
                    "Failed to send fallback WhatsApp message",
                    extra={"error": str(send_error), "phone_number": phone_number},
                )

        return JSONResponse(content={"status": "error"}, status_code=500)
