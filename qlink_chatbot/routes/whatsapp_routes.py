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


def _extract_gupshup_message(request_data: dict) -> dict:
    """Return a normalized inbound message from Gupshup v2 callbacks."""
    if request_data.get("type") != "message":
        return {}

    payload = request_data.get("payload") or {}
    message_type = payload.get("type", "")
    content = payload.get("payload") or {}

    text = ""
    if message_type == "text":
        text = content.get("text", "")
    elif message_type in {"button_reply", "list_reply"}:
        text = content.get("title") or content.get("text") or content.get("postbackText", "")

    return {
        "from": payload.get("source", ""),
        "text": (text or "").strip(),
        "name": (payload.get("sender") or {}).get("name", ""),
    }


def _extract_username(whatsapp_event: dict, fallback_name: str = "") -> str:
    contacts = whatsapp_event.get("contacts", [])
    if not contacts:
        return fallback_name
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


@whatsapp_router.post("/gupshup/message/hc")
async def gupshup_messages(data: Request):
    """Gupshup webhook endpoint to receive and reply to WhatsApp messages."""
    request_data = await data.json()
    logger.info("Gupshup request received", extra={"data": request_data})

    phone_number = ""

    try:
        gupshup_message = _extract_gupshup_message(request_data)
        if request_data.get("type") and request_data.get("type") != "message":
            logger.info(
                "Ignoring non-message Gupshup callback",
                extra={"type": request_data.get("type")},
            )
            return {"status": "success", "ignored_type": request_data.get("type")}

        whatsapp_event = _extract_event(request_data)

        statuses = whatsapp_event.get("statuses", [])
        if statuses:
            status_payload = statuses[0]
            status = status_payload.get("type") or status_payload.get("status")
            logger.info("Ignoring status callback", extra={"status": status})
            return {"status": "success", "ignored_status": status}

        incoming_messages = whatsapp_event.get("messages", [])
        if gupshup_message:
            phone_number = gupshup_message.get("from", "")
            whatsapp_username = gupshup_message.get("name", "")
            user_text = gupshup_message.get("text", "")
        elif incoming_messages:
            incoming_message = incoming_messages[0]
            phone_number = incoming_message.get("from", "")
            whatsapp_username = _extract_username(whatsapp_event)
            user_text = _extract_user_message_text(incoming_message)
        else:
            logger.info("No incoming messages in webhook payload")
            return {"status": "ignored", "reason": "no_messages"}

        if not phone_number:
            return JSONResponse(
                content={"status": "error", "message": "Missing sender number"},
                status_code=400,
            )

        if not user_text:
            logger.info(
                "Ignoring unsupported inbound message type",
                extra={"phone_number": phone_number, "message": request_data},
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

        bot_responses = [{"type": "text", "text": bot_text}]
        dispatch_whatsapp_responses(
            phone_number=phone_number,
            bot_responses=bot_responses,
        )

        return {"status": "success"}

    except Exception as e:
        logger.exception(
            "Exception occurred while handling WhatsApp webhook",
            extra={"exception": str(e), "phone_number": phone_number},
        )

        if phone_number:
            try:
                dispatch_whatsapp_responses(
                    phone_number=phone_number,
                    bot_responses=[
                        {
                            "type": "text",
                            "text": "Unexpected error occurred.",
                        }
                    ],
                )
            except Exception as send_error:
                logger.error(
                    "Failed to send fallback WhatsApp message",
                    extra={"error": str(send_error), "phone_number": phone_number},
                )

        return JSONResponse(content={"status": "error"}, status_code=500)
