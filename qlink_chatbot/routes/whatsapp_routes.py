import asyncio
import re

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import Response

from qlink_chatbot.database.mongo_utils import (
    create_session,
    get_previous_search,
    get_session_by_id,
    save_message,
    save_previous_search,
    save_user_name,
    whatsapp_status_events_collection,
)
from qlink_chatbot.utils.jaipur_rugs_api import (
    CALLING_CODE_TO_CURRENCY,
    COMMON_COLORS,
    KNOWN_CONSTRUCTIONS,
    KNOWN_MATERIALS,
    KNOWN_STYLES,
    jaipur_rugs_product_search,
)
from qlink_chatbot.utils.logger_config import logger
from qlink_chatbot.whatsapp_functions.dispatch import dispatch_whatsapp_responses
from qlink_chatbot.whatsapp_functions.send_typing_indicator import typing_indicator_loop

whatsapp_router = APIRouter()
WHATSAPP_COLLECTION_NAME = "users_whatsapp"

_IMAGE_NOT_SUPPORTED_RESPONSE = (
    "I'm sorry, I'm not able to identify or process images at this time.\n\n"
    "For assistance, please reach out to us:\n"
    "- Email: shop@jaipurrugs.com\n"
    "- India: +91 8000295928 (WhatsApp available)\n"
    "- International: +91 7412 060 022 (WhatsApp available)"
)
_MEDIA_SENTINEL = "__MEDIA_MESSAGE__"

# Calling codes sorted longest-first so "971" (UAE) matches before "9" prefix
_CALLING_CODE_SORTED = sorted(CALLING_CODE_TO_CURRENCY.keys(), key=len, reverse=True)

_CURRENCY_SYMBOLS: dict[str, str] = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£",
    "AUD": "A$", "SGD": "S$", "CHF": "CHF ", "AED": "AED ",
}

_GREETING_WORDS = {"hi", "hello", "hey", "hii", "helo", "namaste", "greetings", "howdy"}
_CONTACT_WORDS = {
    "contact", "email", "support", "human", "agent", "representative",
    "call", "reach", "team", "customer care",
}
_PRODUCT_TRIGGER_WORDS = {
    "rug", "rugs", "carpet", "carpets", "show", "find", "search", "looking",
    "want", "need", "buy", "shop", "get", "recommend", "suggest", "available",
    "collection", "price", "cost", "display", "see", "view",
}

_SHOW_MORE_RE = re.compile(
    r'\b(show\s*(more|next|other)|more\s*(rug|rugs|option|options|product|products)?'
    r'|next\s*(rug|rugs|option|options)|other\s*(rug|rugs|option|options))\b',
    re.IGNORECASE,
)

_SIZE_RE = re.compile(r'\b(\d+)\s*[xX×]\s*(\d+)\b')
_PRICE_RE = re.compile(r'\b(inr|usd|eur|gbp|aud|chf|sgd|aed)\s*([\d,]+)\b', re.IGNORECASE)


def _currency_from_phone(phone: str) -> str:
    digits = re.sub(r'\D', '', phone)
    if digits.startswith('00'):
        digits = digits[2:]
    for code in _CALLING_CODE_SORTED:
        if digits.startswith(code):
            return CALLING_CODE_TO_CURRENCY[code]
    return "INR"


def _detect_intent(text: str) -> str:
    lower = text.lower()
    words = set(re.findall(r'\b\w+\b', lower))

    if words & _CONTACT_WORDS:
        return "contact"

    has_product_word = bool(words & _PRODUCT_TRIGGER_WORDS)
    has_color = any(re.search(rf'\b{re.escape(c)}\b', lower) for c in COMMON_COLORS)
    has_size = bool(_SIZE_RE.search(text))
    has_material = any(re.search(rf'\b{re.escape(m)}\b', lower) for m in KNOWN_MATERIALS)
    has_construction = any(c in lower for c in KNOWN_CONSTRUCTIONS)
    has_style = any(re.search(rf'\b{re.escape(s)}\b', lower) for s in KNOWN_STYLES)

    if any([has_product_word, has_color, has_size, has_material, has_construction, has_style]):
        return "product_search"

    if words & _GREETING_WORDS:
        return "greeting"

    return "product_search"


def _is_show_more(text: str) -> bool:
    return bool(_SHOW_MORE_RE.search(text))


def _build_search_keyword(text: str) -> str:
    """Convert natural language to &-joined keyword string for jaipur_rugs_product_search."""
    lower = text.lower()
    parts: list[str] = []

    # Colors (longest match first to prefer "multicolor" over "multi")
    for color in sorted(COMMON_COLORS, key=len, reverse=True):
        if re.search(rf'\b{re.escape(color)}\b', lower):
            parts.append(color)

    # Sizes like 8x10, 9x12
    for m in _SIZE_RE.finditer(text):
        parts.append(f"{m.group(1)}x{m.group(2)}")

    # Constructions (longest first — "hand knotted" before "hand")
    for c in sorted(KNOWN_CONSTRUCTIONS, key=len, reverse=True):
        if c in lower:
            parts.append(c)
            break

    # Materials
    for mat in sorted(KNOWN_MATERIALS, key=len, reverse=True):
        if re.search(rf'\b{re.escape(mat)}\b', lower):
            parts.append(mat)

    # Styles
    for sty in sorted(KNOWN_STYLES, key=len, reverse=True):
        if re.search(rf'\b{re.escape(sty)}\b', lower):
            parts.append(sty)

    # Price filter
    pm = _PRICE_RE.search(lower)
    if pm:
        parts.append(f"{pm.group(1).upper()} {pm.group(2)}")

    if parts:
        return "&".join(dict.fromkeys(parts))  # deduplicate, preserve order

    # Fallback: strip filler words and return remaining text
    _FILLER = {
        'i', 'want', 'need', 'show', 'me', 'find', 'a', 'an', 'the', 'some',
        'please', 'can', 'you', 'get', 'looking', 'for', 'rug', 'rugs',
        'carpet', 'carpets', 'give', 'do', 'have', 'any', 'buy',
    }
    clean = [w for w in re.findall(r'\b\w+\b', lower) if w not in _FILLER]
    return " ".join(clean) or text


def _shown_product_skus(previous_searches: list) -> set[str]:
    skus: set[str] = set()
    for search in (previous_searches or []):
        if not isinstance(search, dict):
            continue
        for product in (search.get("results") or []):
            if not isinstance(product, dict):
                continue
            sku = (product.get("SKU") or "").strip().upper()
            if sku:
                skus.add(sku)
            url = product.get("url", "")
            if url and "barcode=" in url:
                bc = url.rsplit("barcode=", 1)[-1].split("&", 1)[0].strip().upper()
                if bc:
                    skus.add(bc)
    return skus


def _format_products_for_whatsapp(products: list, currency: str) -> list[dict]:
    """Convert product dicts to WhatsApp interactive_cta message dicts."""
    messages: list[dict] = []
    sym = _CURRENCY_SYMBOLS.get(currency, currency + " ")

    for product in products:
        if not isinstance(product, dict):
            continue

        name = (product.get("name") or product.get("collection") or "Jaipur Rug").strip()
        image_url = product.get("image", "")
        url = product.get("url", "")
        size = product.get("size", "")
        material = (product.get("material") or product.get("fabric", "")).strip()
        construction = product.get("construction", "")

        mrp = product.get("mrp", {})
        price_val = mrp.get(currency)
        if price_val:
            try:
                price_str = f"{sym}{float(price_val):,.0f} {currency}"
            except (TypeError, ValueError):
                price_str = f"{sym}{price_val} {currency}"
        else:
            price_str = "Price on request"

        lines = [f"*{name}*"]
        if size:
            lines.append(f"- Size: {size}")
        if material:
            lines.append(f"- Material: {material}")
        lines.append(f"- Price: {price_str}")
        if construction:
            lines.append(f"- Construction: {construction}")

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
            messages.append({
                "type": "interactive_cta",
                "button_url": url,
                "caption": caption,
                "button_text": "View Product",
            })

    return messages


# ── Webhook payload parsers (unchanged) ─────────────────────────────────────

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

    phone = payload.get("source", "") or payload.get("sender", {}).get("phone", "")
    if not phone:
        return {}
    return {
        "from": phone,
        "text": (text or "").strip(),
        "name": (payload.get("sender") or {}).get("name", ""),
        "message_id": payload.get("id", "") or content.get("id", ""),
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
        elif incoming_messages:
            incoming_message = incoming_messages[0]
            phone_number = incoming_message.get("from", "")
            whatsapp_username = _extract_username(whatsapp_event)
            user_text = _extract_user_message_text(incoming_message)
            message_id = incoming_message.get("id", "")
        else:
            logger.info("No incoming messages in webhook payload")
            return

        if not phone_number or not user_text:
            logger.info("Skipping — missing phone or text",
                        extra={"phone_number": phone_number})
            return

        if user_text == _MEDIA_SENTINEL:
            dispatch_whatsapp_responses(
                phone_number=phone_number,
                bot_responses=[{"type": "text", "text": _IMAGE_NOT_SUPPORTED_RESPONSE}],
            )
            return

        session_id = phone_number.lower()
        session = get_session_by_id(session_id=session_id,
                                    collection_name=WHATSAPP_COLLECTION_NAME)

        if not session:
            create_session(session_id=session_id, country_code="",
                           name=whatsapp_username, is_ai=True,
                           collection_name=WHATSAPP_COLLECTION_NAME)
            session = {"chat_history": [], "country_code": ""}
        elif whatsapp_username and whatsapp_username != session.get("user_name", ""):
            save_user_name(session_id=session_id, name=whatsapp_username,
                           collection_name=WHATSAPP_COLLECTION_NAME)

        save_message(session_id=session_id, role="user", content=user_text,
                     collection_name=WHATSAPP_COLLECTION_NAME)

        if not session.get("is_ai", True):
            logger.info("Human agent active — skipping AI response",
                        extra={"phone_number": phone_number})
            return

        # Currency derived from phone calling code — no IP geo needed for WhatsApp
        currency = _currency_from_phone(phone_number)

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(typing_indicator_loop(message_id, stop_typing))

        responses: list[dict] = []
        bot_log = ""
        keyword = ""

        try:
            if _is_show_more(user_text):
                previous_searches = get_previous_search(
                    session_id, collection_name=WHATSAPP_COLLECTION_NAME
                )
                if previous_searches:
                    keyword = (previous_searches[-1] or {}).get("keyword", "")
                    shown_skus = _shown_product_skus(previous_searches)
                    products = await jaipur_rugs_product_search(
                        keyword, client_ip="", country_code="", exclude_skus=shown_skus
                    )
                    if isinstance(products, list) and products:
                        save_previous_search(session_id, keyword, products,
                                             collection_name=WHATSAPP_COLLECTION_NAME)
                        responses = _format_products_for_whatsapp(products, currency)
                        responses.append({
                            "type": "interactive_cta",
                            "button_url": "https://www.jaipurrugs.com/in/search",
                            "caption": "Browse the full collection on our website.",
                            "button_text": "Search More Rugs",
                        })
                        bot_log = f"Showed more products for: {keyword}"
                    else:
                        responses = [{"type": "text", "text":
                            "I couldn't find more rugs matching your search. "
                            "Try a different color, size, or material!"}]
                        bot_log = "No more products found."
                else:
                    responses = [{"type": "text", "text":
                        "What kind of rug are you looking for? "
                        "Tell me the color, size, or material and I'll find the best options."}]
                    bot_log = "Show more requested but no previous search."

            else:
                intent = _detect_intent(user_text)

                if intent == "contact":
                    responses = [{"type": "text", "text":
                        "*Jaipur Rugs Support*\n\n"
                        "- Email: shop@jaipurrugs.com\n"
                        "- India: +91 8000295928 (WhatsApp)\n"
                        "- International: +91 7412 060 022 (WhatsApp)\n\n"
                        "Our team is available Mon–Sat, 10 AM – 6 PM IST."}]
                    bot_log = "Shared contact information."

                elif intent == "greeting":
                    responses = [{"type": "text", "text":
                        "Hello! Welcome to *Jaipur Rugs*\n\n"
                        "I can help you find the perfect rug. Just tell me:\n"
                        "- Color (e.g., red, blue, ivory)\n"
                        "- Size (e.g., 8x10, 6x9)\n"
                        "- Material (e.g., wool, silk)\n"
                        "- Style (e.g., modern, traditional)\n\n"
                        "What would you like to explore?"}]
                    bot_log = "Sent greeting."

                else:  # product_search
                    keyword = _build_search_keyword(user_text)
                    previous_searches = get_previous_search(
                        session_id, collection_name=WHATSAPP_COLLECTION_NAME
                    )
                    shown_skus = _shown_product_skus(previous_searches)

                    logger.info("WhatsApp product search",
                                extra={"keyword": keyword, "exclude_count": len(shown_skus)})

                    products = await jaipur_rugs_product_search(
                        keyword, client_ip="", country_code="", exclude_skus=shown_skus
                    )

                    if isinstance(products, list) and products:
                        save_previous_search(session_id, keyword, products,
                                             collection_name=WHATSAPP_COLLECTION_NAME)
                        responses = _format_products_for_whatsapp(products, currency)
                        responses.append({
                            "type": "interactive_cta",
                            "button_url": "https://www.jaipurrugs.com/in/search",
                            "caption": "Browse the full collection on our website.",
                            "button_text": "Search More Rugs",
                        })
                        bot_log = f"Showed {len(products)} products for: {keyword}"
                    else:
                        responses = [{"type": "text", "text":
                            "I couldn't find rugs matching your search. "
                            "Try describing the color, size, or material you're looking for!"}]
                        bot_log = f"No products found for: {keyword}"

        finally:
            stop_typing.set()
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        save_message(session_id=session_id, role="assistant", content=bot_log,
                     collection_name=WHATSAPP_COLLECTION_NAME)

        dispatch_whatsapp_responses(phone_number=phone_number, bot_responses=responses)

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


@whatsapp_router.post("/gupshup/message/hc")
async def gupshup_messages(data: Request, background_tasks: BackgroundTasks):
    """Gupshup webhook — returns empty 200 immediately, processes in background."""
    request_data = await data.json()
    logger.info("Gupshup request received", extra={"data": request_data})
    background_tasks.add_task(_process_message, request_data)
    return Response(status_code=200)
