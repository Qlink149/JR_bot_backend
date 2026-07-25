import asyncio
import re

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import Response

from qlink_chatbot.agent.chat_agent import chat_agent
from qlink_chatbot.database.mongo_utils import (
    create_session,
    get_previous_search,
    get_session_by_id,
    save_message,
    save_user_name,
    try_mark_whatsapp_message_processed,
    whatsapp_status_events_collection,
)
from qlink_chatbot.utils.jaipur_rugs_api import CALLING_CODE_TO_CURRENCY
from qlink_chatbot.utils.logger_config import logger
from qlink_chatbot.utils.support_contacts import (
    SHOP_EMAIL,
    country_from_phone,
    general_support_phone,
)
from qlink_chatbot.utils.search_session import (
    build_search_intro,
    products_honesty_note,
    products_have_size_relaxed,
)
from qlink_chatbot.utils.search_trace import log_search_turn
from qlink_chatbot.utils.whatsapp_guards import (
    is_inbound_rate_limited,
    release_phone_lock,
    try_acquire_phone_lock,
)
from qlink_chatbot.utils.whatsapp_images import get_whatsapp_safe_image_url
from qlink_chatbot.whatsapp_functions.dispatch import dispatch_whatsapp_responses
from qlink_chatbot.whatsapp_functions.send_typing_indicator import typing_indicator_loop

whatsapp_router = APIRouter()
WHATSAPP_COLLECTION_NAME = "users_whatsapp"

_MEDIA_SENTINEL = "__MEDIA_MESSAGE__"

_CALLING_CODE_SORTED = sorted(CALLING_CODE_TO_CURRENCY.keys(), key=len, reverse=True)

_CURRENCY_SYMBOLS: dict[str, str] = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£",
    "AUD": "A$", "SGD": "S$", "CHF": "CHF ", "AED": "AED ",
}

# Regex patterns for markdown → WhatsApp conversion
_MD_SEARCH_MORE = re.compile(r'\[🔍 Search More Rugs\]\([^)]+\)', re.IGNORECASE)
_MD_EMPHASIS = re.compile(r'(\*{1,2})([^*\n]+)\1')
_MD_HEADER = re.compile(r'^#{1,6}\s+', re.MULTILINE)
# Strip markdown images first (WhatsApp cannot render ![alt](url) as media).
_MD_IMAGE = re.compile(r'!\[[^\]]*\]\([^)]+\)')
_MD_LINK = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
_MD_BULLET = re.compile(r'^- ', re.MULTILINE)


def _currency_from_phone(phone: str) -> str:
    digits = re.sub(r'\D', '', phone)
    if digits.startswith('00'):
        digits = digits[2:]
    for code in _CALLING_CODE_SORTED:
        if digits.startswith(code):
            return CALLING_CODE_TO_CURRENCY[code]
    return "INR"


def _markdown_to_whatsapp(text: str) -> str:
    """Convert AI markdown response to WhatsApp message format.

    WhatsApp formatting: *bold*, _italic_, ~strikethrough~, ```code```
    Markdown formatting: **bold**, *italic*, # headers, [text](url), - lists
    """
    # Remove the "Search More Rugs" CTA — sent as a separate button
    text = _MD_SEARCH_MORE.sub('', text).strip()

    # Drop markdown images — they never render as media on WhatsApp
    # (product images are sent as interactive CTA headers separately).
    text = _MD_IMAGE.sub('', text)

    # Convert **bold** → *bold* and *italic* → _italic_ in one pass
    def _convert_emphasis(m: re.Match) -> str:
        markers, content = m.group(1), m.group(2)
        return f'*{content}*' if len(markers) == 2 else f'_{content}_'

    text = _MD_EMPHASIS.sub(_convert_emphasis, text)

    # Remove markdown headers (## Heading → Heading)
    text = _MD_HEADER.sub('', text)

    # Convert [text](url) links → "text: url" (plain text)
    def _link_to_text(m: re.Match) -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        return url if label == url else f'{label}: {url}'

    text = _MD_LINK.sub(_link_to_text, text)

    # Convert markdown bullet lists to WhatsApp style
    text = _MD_BULLET.sub('• ', text)

    # Collapse blank lines left by stripped images
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def _whatsapp_text_after_product_cards(ai_text: str) -> str:
    """Short intro when product CTA cards already carry the details + images."""
    text = _markdown_to_whatsapp(ai_text or "")
    if not text:
        return "Here are some rugs I found for you:"
    # Prefer the first short paragraph without product URLs / leftover link dumps.
    first = text.split("\n\n")[0].strip()
    if first and "http" not in first.lower() and len(first) <= 220:
        return first
    return "Here are some rugs I found for you:"


def _format_products_for_whatsapp(products: list, currency: str) -> list[dict]:
    """Convert product dicts to WhatsApp interactive_cta message dicts."""
    messages: list[dict] = []
    sym = _CURRENCY_SYMBOLS.get(currency, currency + " ")
    ordinal = 0

    for product in products:
        if not isinstance(product, dict):
            continue
        ordinal += 1

        name = (product.get("name") or product.get("collection") or "Jaipur Rug").strip()
        raw_image = product.get("image") or product.get("image_url") or ""
        image_url = get_whatsapp_safe_image_url(raw_image)
        url = product.get("url", "")
        size = product.get("size", "")
        material = (product.get("material") or product.get("fabric", "")).strip()
        construction = product.get("construction", "")

        # Prefer search display_price (matches web/agent); fallback to mrp[currency].
        price_str = (product.get("display_price") or "").strip()
        if not price_str:
            mrp = product.get("mrp", {}) or {}
            price_val = mrp.get(currency)
            if price_val:
                try:
                    price_str = f"{sym}{float(price_val):,.0f} {currency}"
                except (TypeError, ValueError):
                    price_str = f"{sym}{price_val} {currency}"
            else:
                price_str = "Price on request"

        lines = [f"*{ordinal}. {name}*"]
        if size:
            lines.append(f"• Size: {size}")
        if material:
            lines.append(f"• Material: {material}")
        lines.append(f"• Price: {price_str}")
        if construction:
            lines.append(f"• Construction: {construction}")
        if product.get("size_relaxed"):
            lines.append("• Closest available size (exact size not in stock)")

        caption = "\n".join(lines)

        if image_url and url:
            messages.append({
                "type": "interactive_cta",
                "image_url": image_url,
                "button_url": url,
                "caption": caption,
                "button_text": "View Product",
            })
        elif url:
            if raw_image and not image_url:
                logger.warning(
                    f"[WA] Skipping invalid product image for {name!r}: {raw_image!r}"
                )
            messages.append({
                "type": "interactive_cta",
                "button_url": url,
                "caption": caption,
                "button_text": "View Product",
            })

    return messages


# ── Webhook payload parsers ──────────────────────────────────────────────────

def _extract_event(request_data: dict) -> dict:
    entry = request_data.get("entry", [])
    changes = entry[0].get("changes", []) if entry else []
    return changes[0].get("value", {}) if changes else {}


def _extract_gupshup_message(request_data: dict) -> dict:
    event_type = request_data.get("type")
    if event_type and event_type != "message":
        return {}

    payload = request_data.get("payload") or {}
    if not payload and request_data.get("source") and request_data.get("type"):
        payload = request_data
    message_type = (payload.get("type") or request_data.get("payload", {}).get("type") or "").strip()
    content = payload.get("payload")
    if not isinstance(content, dict):
        content = payload

    text = ""
    image_url = ""
    if message_type in {"text", "txt"}:
        text = content.get("text", "")
    elif message_type in {"button_reply", "list_reply", "button"}:
        text = (
            content.get("title")
            or content.get("text")
            or content.get("postbackText", "")
        )
    elif message_type in {"image", "video", "audio", "document", "sticker"}:
        text = _MEDIA_SENTINEL
        if message_type == "image":
            image_url = (
                content.get("url")
                or content.get("link")
                or (content.get("image") or {}).get("url")
                or ""
            )
            caption = (content.get("caption") or "").strip()
            if caption:
                text = caption

    phone = payload.get("source", "") or payload.get("sender", {}).get("phone", "")
    if not phone:
        return {}
    return {
        "from": phone,
        "text": (text or "").strip(),
        "name": (payload.get("sender") or {}).get("name", ""),
        "message_id": payload.get("id", "") or content.get("id", ""),
        "image_url": (image_url or "").strip(),
        "message_type": message_type,
    }


def _extract_username(whatsapp_event: dict, fallback_name: str = "") -> str:
    contacts = whatsapp_event.get("contacts", [])
    if not contacts:
        return fallback_name
    return contacts[0].get("profile", {}).get("name", "")


def _extract_user_message_text(message_payload: dict) -> str:
    if message_payload.get("type") in {"image", "video", "audio", "document", "sticker"}:
        return _MEDIA_SENTINEL

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


# ── Core message handler ─────────────────────────────────────────────────────

async def _process_message(request_data: dict) -> None:
    """Process the inbound WhatsApp message in the background after returning 200."""
    phone_number = ""
    lock_held = False
    try:
        gupshup_message = _extract_gupshup_message(request_data)

        if request_data.get("type") and request_data.get("type") != "message":
            logger.info("Ignoring non-message Gupshup callback",
                        extra={"type": request_data.get("type")})
            return

        whatsapp_event = _extract_event(request_data)

        statuses = whatsapp_event.get("statuses", [])
        if statuses:
            status = statuses[0].get("type") or statuses[0].get("status")
            try:
                whatsapp_status_events_collection.insert_one(
                    {"status": status, "statuses": statuses, "raw_event": whatsapp_event}
                )
            except Exception as status_save_error:
                logger.warning("Failed to persist WhatsApp status callback",
                               extra={"error": str(status_save_error)})
            logger.info("Ignoring status callback", extra={"status": status})
            return

        incoming_messages = whatsapp_event.get("messages", [])
        if gupshup_message:
            phone_number = gupshup_message.get("from", "")
            whatsapp_username = gupshup_message.get("name", "")
            user_text = gupshup_message.get("text", "")
            message_id = gupshup_message.get("message_id", "")
            image_url = gupshup_message.get("image_url", "") or ""
            message_type = gupshup_message.get("message_type", "")
        elif incoming_messages:
            incoming_message = incoming_messages[0]
            phone_number = incoming_message.get("from", "")
            whatsapp_username = _extract_username(whatsapp_event)
            user_text = _extract_user_message_text(incoming_message)
            message_id = incoming_message.get("id", "")
            image_url = ""
            message_type = incoming_message.get("type", "")
            if message_type == "image":
                image_block = incoming_message.get("image") or {}
                image_url = (image_block.get("url") or image_block.get("link") or "").strip()
                if user_text == _MEDIA_SENTINEL:
                    user_text = (image_block.get("caption") or "").strip() or (
                        "Please review this custom rug design image."
                    )
        else:
            logger.info("No incoming messages in webhook payload")
            return

        if not phone_number or (not user_text and not image_url):
            logger.info("Skipping — missing phone or text",
                        extra={"phone_number": phone_number})
            return

        if not try_mark_whatsapp_message_processed(
            message_id=message_id,
            phone=phone_number,
            message_text=user_text,
        ):
            logger.info(f"[WA] Duplicate message skipped id={message_id}")
            return

        if is_inbound_rate_limited(phone_number):
            dispatch_whatsapp_responses(
                phone_number=phone_number,
                bot_responses=[{
                    "type": "text",
                    "text": "Please wait a moment before sending another message.",
                }],
            )
            return

        if not try_acquire_phone_lock(phone_number):
            dispatch_whatsapp_responses(
                phone_number=phone_number,
                bot_responses=[{
                    "type": "text",
                    "text": "Still working on your previous message — one moment please.",
                }],
            )
            return
        lock_held = True

        logger.info(f"[WA-IN] phone={phone_number} name={whatsapp_username!r} msg={user_text!r} image={'yes' if image_url else 'no'}")

        # Non-image media still unsupported
        if user_text == _MEDIA_SENTINEL and not image_url:
            support = general_support_phone("", phone_number)
            dispatch_whatsapp_responses(
                phone_number=phone_number,
                bot_responses=[{
                    "type": "text",
                    "text": (
                        "I can review custom rug design photos when you send an image. "
                        f"For other file types, email {SHOP_EMAIL} or WhatsApp {support}."
                    ),
                }],
            )
            return

        if image_url and (not user_text or user_text == _MEDIA_SENTINEL):
            user_text = "Please review this custom rug design image."

        session_id = phone_number.lower()
        session = get_session_by_id(session_id=session_id,
                                    collection_name=WHATSAPP_COLLECTION_NAME)
        country_code = country_from_phone(phone_number)

        if not session:
            logger.info(f"[WA] New session created for {phone_number}")
            create_session(session_id=session_id, country_code=country_code,
                           name=whatsapp_username, is_ai=True,
                           collection_name=WHATSAPP_COLLECTION_NAME)
            session = {"chat_history": [], "country_code": country_code}
        else:
            history_len = len(session.get("chat_history") or [])
            logger.info(f"[WA] Existing session for {phone_number} — history_msgs={history_len} is_ai={session.get('is_ai', True)}")
            if whatsapp_username and whatsapp_username != session.get("user_name", ""):
                save_user_name(session_id=session_id, name=whatsapp_username,
                               collection_name=WHATSAPP_COLLECTION_NAME)
            country_code = session.get("country_code") or country_code

        save_message(session_id=session_id, role="user", content=user_text,
                     collection_name=WHATSAPP_COLLECTION_NAME)
        # Re-fetch so chat_history includes the message we just saved
        session = get_session_by_id(session_id=session_id,
                                    collection_name=WHATSAPP_COLLECTION_NAME) or session

        if not session.get("is_ai", True):
            logger.info(f"[WA] Human agent active for {phone_number} — skipping AI")
            return

        currency = _currency_from_phone(phone_number)
        logger.info(f"[WA] Detected currency={currency} country={country_code} for {phone_number}")

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(typing_indicator_loop(message_id, stop_typing))

        ai_text = ""
        responses: list[dict] = []
        latest_products: list = []
        new_products_found = False
        try:
            searches_before = get_previous_search(session_id,
                                                  collection_name=WHATSAPP_COLLECTION_NAME)
            count_before = len(searches_before) if searches_before else 0

            logger.info(f"[WA] Calling chat_agent for {phone_number} | msg={user_text!r}")
            ai_text = await chat_agent(
                chat_history=session.get("chat_history", []),
                user_message=user_text,
                session_id=session_id,
                country_code=country_code,
                client_ip="",
                collection_name=WHATSAPP_COLLECTION_NAME,
                detected_currency=currency,
                image_url=image_url,
            )
            logger.info(f"[WA-AI] phone={phone_number} ai_response={ai_text!r}")

            searches_after = get_previous_search(session_id,
                                                 collection_name=WHATSAPP_COLLECTION_NAME)
            new_products_found = searches_after and len(searches_after) > count_before
            logger.info(f"[WA] Product search triggered={new_products_found} (before={count_before} after={len(searches_after) if searches_after else 0})")

            product_cards_sent = False
            latest_products = []
            if new_products_found:
                latest_entry = searches_after[-1] or {}
                latest_products = latest_entry.get("results", []) or []
                if latest_products:
                    product_cards = _format_products_for_whatsapp(latest_products, currency)
                    logger.info(
                        f"[WA] Sending {len(product_cards)} product CTA card(s) "
                        f"to {phone_number}"
                    )
                    responses.extend(product_cards)
                    responses.append({
                        "type": "interactive_cta",
                        "button_url": "https://www.jaipurrugs.com/in/search",
                        "caption": "Browse the full collection on our website.",
                        "button_text": "Search More Rugs",
                    })
                    product_cards_sent = bool(product_cards)

            # When CTA cards carry images + details, don't dump markdown product
            # blocks (including broken ![Rug Image] lines) into the text reply.
            if product_cards_sent:
                wa_text = _whatsapp_text_after_product_cards(ai_text or "")
                # Always surface the single honesty note when search was relaxed.
                if products_honesty_note(latest_products):
                    wa_text = build_search_intro(products=latest_products)
            else:
                wa_text = _markdown_to_whatsapp(ai_text or "")
            if wa_text:
                # Intro text first, then product cards (Kisna-style ordering).
                if product_cards_sent:
                    responses.insert(0, {"type": "text", "text": wa_text})
                else:
                    responses.append({"type": "text", "text": wa_text})

            logger.info(f"[WA-OUT] phone={phone_number} dispatching {len(responses)} message(s): types={[r.get('type') for r in responses]}")

        finally:
            stop_typing.set()
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        save_message(session_id=session_id, role="assistant", content=ai_text,
                     collection_name=WHATSAPP_COLLECTION_NAME)

        dispatch_errors = dispatch_whatsapp_responses(
            phone_number=phone_number, bot_responses=responses
        )
        wa_products = latest_products if latest_products else []
        log_search_turn(
            session_id=session_id,
            channel="whatsapp",
            products_found=len(wa_products) if new_products_found else 0,
            wa_cards_sent=sum(1 for r in responses if r.get("type") == "interactive_cta"),
            dispatch_errors=dispatch_errors if isinstance(dispatch_errors, int) else 0,
            size_relaxed=products_have_size_relaxed(wa_products) if wa_products else False,
            fallback_note=next(
                (p.get("fallback_note") or "" for p in wa_products if p.get("fallback_note")),
                "",
            ),
            strategy=next(
                (p.get("search_strategy") or "" for p in wa_products if p.get("search_strategy")),
                "",
            ),
        )

    except Exception as e:
        logger.exception("Exception in background message processing",
                         extra={"exception": str(e), "phone_number": phone_number})
        if phone_number:
            try:
                dispatch_whatsapp_responses(
                    phone_number=phone_number,
                    bot_responses=[{"type": "text", "text": "Unexpected error occurred. Please try again."}],
                )
            except Exception as send_error:
                logger.error("Failed to send fallback message",
                             extra={"error": str(send_error), "phone_number": phone_number})
    finally:
        if lock_held and phone_number:
            release_phone_lock(phone_number)


@whatsapp_router.post("/gupshup/message/hc")
async def gupshup_messages(data: Request, background_tasks: BackgroundTasks):
    """Gupshup webhook — returns empty 200 immediately, processes in background."""
    request_data = await data.json()
    logger.info("Gupshup request received", extra={"data": request_data})
    background_tasks.add_task(_process_message, request_data)
    return Response(status_code=200)
