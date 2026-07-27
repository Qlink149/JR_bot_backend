import json
import os
import re
from datetime import datetime, timedelta, timezone

from openai import AsyncOpenAI

from qlink_chatbot.agent.utils.chat_agent_prompts import build_system_prompt
from qlink_chatbot.database.mongo_utils import (
    clear_search_buffer,
    get_previous_search,
    get_search_buffer,
    raise_alert,
    return_system_prompt,
    save_previous_search,
    save_search_buffer,
    save_user_name,
    user_name,
)
from qlink_chatbot.database.pinecone_utils import fetch_similar_sessions
from qlink_chatbot.utils.agent_availability import get_agent_status
from qlink_chatbot.utils.jaipur_rugs_api import (
    jaipur_rugs_product_search,
    normalise_search_keyword,
    resolve_search_keyword,
)
from qlink_chatbot.utils.jr_search_currency import price_filter_to_keyword
from qlink_chatbot.utils.jr_search_keywords import normalise_keyword
from qlink_chatbot.utils.jr_search_llm_extract import (
    _message_has_catalog_attrs,
    _should_ignore_previous_search,
    resolve_hygienic_search_keyword,
    resolved_keyword_for_memory,
    serialise_for_json,
)
from qlink_chatbot.utils.logger_config import logger
from qlink_chatbot.utils.product_format import (
    format_product_search_message,
    format_single_product_detail,
)
from qlink_chatbot.utils.search_session import (
    SEARCH_PAGE_SIZE,
    ensure_search_honesty_prefix,
    latest_search_has_results,
)
from qlink_chatbot.utils.search_trace import log_search_turn
from qlink_chatbot.utils.store_locations import JAIPUR_RUGS_STORE_LOCATIONS, search_store_locations
from qlink_chatbot.utils.support_contacts import (
    SHOP_EMAIL,
    general_support_phone,
    support_contacts_blurb,
)

API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=API_KEY) if API_KEY else None

output_schema = {
    "format": {
        "type": "json_schema",
        "name": "general_agent_schema_v1",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message to send to the user."
                }
            },
            "required": ["message"],
            "additionalProperties": False
        }
    }
}




# Tool definition
tools = [
    {
        "type": "function",
        "name": "jaipur_rugs_product_search",
        "description": "Search rugs from Jaipur Rugs API and return formatted product details like URL, weight, fabric, image, description, and MRP values.",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Search keyword built ONLY from the CURRENT user message. Use '&' to AND attributes. Supported: color, shape, size (8x10), material, construction, style/pattern, room, price, collection/design names (e.g. aurelia), AND mega-menu tags: 'new arrival' / 'bestsellers' / 'outdoor' / 'antique' / 'rug swatch'. Soft phrasing still counts — pass the ask as-is or as attributes: 'is there any new arrival' → 'new arrival'; 'aurelia in red' → 'aurelia&red'. Examples: 'bestsellers', 'outdoor&under INR 30000', 'beige', 'blue&round', 'red&8x10', 'aurelia&red', 'new arrival'. NEVER copy colors/sizes/materials/rooms/collections from earlier turns unless the user restates them or clearly refines ('same but cheaper', 'also in wool')."
                },
                "currency": {
                    "type": "string",
                    "description": "Display currency for product prices. Rules: (1) If the user mentions a specific currency in a price query — e.g. 'above USD 1000', 'under GBP 500', 'between USD 200 to USD 800' — pass that currency here (e.g. 'USD', 'GBP') AND include the price in the keyword. (2) If the user explicitly asks to see prices in a currency — e.g. 'show prices in EUR' — pass that currency. (3) Otherwise leave empty and the system will use the user's local currency."
                }
            },
            "required": ["keyword"]
        }
    },
    {
        "type": "function",
        "name": "get_previous_search",
        "description": "Retrieve the user's last 3–4 previous searches if they ask for it.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "User's username"}
            },
            "required": ["session_id"]
        }
    },
    {
        "type": "function",
        "name": "search_kb",
        "description": (
            "Search the knowledge base for Jaipur Rugs policy/FAQ content from jaipurrugs.com "
            "(returns, shipping, payments, gifting, rug care) plus agent learnings. "
            "Use for policy questions — not for product catalog search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query text representing what the user is looking for."
                },
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "search_store_locations",
        "description": "Search verified Jaipur Rugs showroom/store locations, addresses, phone numbers, and emails. MUST be called for ANY question about stores or physical presence — including 'do you have stores?', 'any retail store?', 'where can I see rugs in person?', city/country store queries, address, directions, or timing. Never answer store questions from memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "City, country, area, or 'all stores' for a general query. Examples: 'Delhi', 'Mumbai', 'Bengaluru', 'London', 'all stores'."
                },
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "raise_agent_alert",
        "description": "Raise an alert for a human agent to take over when the assistant cannot answer, needs support, or the user wants a callback. For callbacks, always include callback_phone when the user shared a number.",
        "parameters": {
            "type": "object",
            "properties": {
                "alert": {
                    "type": "string",
                    "description": "Short one-line description of why agent assistance is needed."
                },
                "callback_phone": {
                    "type": "string",
                    "description": "Phone number the user wants to be called on (include country code if provided)."
                },
                "callback_requested": {
                    "type": "boolean",
                    "description": "True when the user asked for a phone callback."
                }
            },
            "required": ["alert"]
        }
    },
]


def format_recent_chat_for_ai(chat_history, limit: int = 10) -> str:
    if not chat_history:
        return ""
    recent_msgs = chat_history[-limit:]
    formatted_lines = []
    for msg in recent_msgs:
        role = msg.get("role", "user").capitalize()
        content = msg.get("content", "")
        formatted_lines.append(f"{role}: {content}")
    return "\n".join(formatted_lines)

def format_recent_products_for_ai(previous_searches, max_products: int = 3) -> str:
    """Return a compact JSON view of the latest shown products for follow-up Q&A."""
    if not previous_searches:
        return "[]"

    latest_search = previous_searches[-1] if isinstance(previous_searches, list) else {}
    if isinstance(latest_search, dict) and latest_search.get("empty"):
        return json.dumps({
            "empty_search": True,
            "keyword": latest_search.get("keyword") or "",
            "products": [],
            "note": "Latest search returned no products — do not answer from older rugs.",
        })

    results = latest_search.get("results", []) if isinstance(latest_search, dict) else []
    if not isinstance(results, list):
        results = []

    compact_products = []
    for idx, product in enumerate(results[:max_products], start=1):
        if not isinstance(product, dict):
            continue
        compact_products.append({
            "ordinal": idx,
            "name": product.get("name", ""),
            "SKU": product.get("SKU", ""),
            "size": product.get("size", ""),
            "shape": product.get("shape", ""),
            "color": product.get("color", ""),
            "pattern": product.get("pattern", ""),
            "material": product.get("material", ""),
            "fabric": product.get("fabric", ""),
            "construction": product.get("construction", ""),
            "weight": product.get("weight", ""),
            "display_price": product.get("display_price", ""),
            "mrp": product.get("mrp", {}),
            "url": product.get("url", ""),
            "size_relaxed": bool(product.get("size_relaxed")),
        })

    return json.dumps(compact_products)


_ORDINAL_MAP = {
    "1": 1, "1st": 1, "first": 1, "one": 1, "#1": 1,
    "2": 2, "2nd": 2, "second": 2, "#2": 2,
    "3": 3, "3rd": 3, "third": 3, "#3": 3,
}


def _resolve_product_ordinal(user_message: str) -> int | None:
    """Parse 1st/2nd/3rd / #2 / 'option 3' — never bare digits in budgets ('3 lakhs')."""
    msg = (user_message or "").strip().lower()
    if not msg:
        return None
    for pattern in (
        # Explicit ordinal words / suffixes only — NOT bare 1/2/3.
        r"\b(?:the\s+)?(1st|first)\b",
        r"\b(?:the\s+)?(2nd|second)\b",
        r"\b(?:the\s+)?(3rd|third)\b",
        r"#\s*(1|2|3)\b",
        r"\b(?:option|product|number|no\.?)\s*#?\s*(1|2|3)\b",
        r"\b(1|2|3)\s*(?:st|nd|rd)\s+(?:one|rug|product|option)\b",
    ):
        m = re.search(pattern, msg, re.IGNORECASE)
        if m:
            token = m.group(1).lower().lstrip("#")
            if token.isdigit():
                return int(token)
            return _ORDINAL_MAP.get(token) or _ORDINAL_MAP.get(m.group(1).lower())
    return None


def _product_at_ordinal(previous_searches, ordinal: int) -> dict | None:
    if not previous_searches or ordinal < 1:
        return None
    latest = previous_searches[-1] if isinstance(previous_searches, list) else {}
    if not isinstance(latest, dict) or latest.get("empty"):
        return None
    results = latest.get("results") or []
    if not isinstance(results, list) or ordinal > len(results):
        return None
    product = results[ordinal - 1]
    return product if isinstance(product, dict) else None


def _merge_search_with_previous(
    keyword: str,
    user_message: str,
    previous_searches,
) -> str:
    """Combine a price-only or only/just refine with the previous catalog keyword.

    Catalog attrs are read from the *user message only* (Kisna-style). Never treat
    a polluted agent tool keyword as evidence of a new shop-by ask.
    """
    last_keyword = _latest_search_keyword(previous_searches)
    if not last_keyword:
        return keyword or user_message

    text = (user_message or "").strip()
    if not text:
        return keyword or user_message

    price_filter, clean_new, _, attribute_filters = normalise_keyword(text)
    catalog_attrs = (
        "color", "color_exact", "shape", "size", "size_cm", "size_category",
        "material", "construction", "pattern", "room", "catalog_tag", "multicolor",
    )
    has_new_catalog_attrs = any(attribute_filters.get(key) for key in catalog_attrs)
    refine_hint = bool(
        re.search(
            r"\b(only|just|instead|make it|rather|also|but)\b",
            text,
            re.IGNORECASE,
        )
    )

    last_price, last_clean, _, _ = normalise_keyword(last_keyword)
    last_price_kw = price_filter_to_keyword(last_price) if last_price else ""

    # "only wool" / "make it rectangle" → glue onto prior search, don't replace.
    if refine_hint and has_new_catalog_attrs and (clean_new or price_filter):
        add_part = clean_new or ""
        if price_filter:
            pk = price_filter_to_keyword(price_filter)
            add_part = f"{add_part}&{pk}" if add_part else pk
        base = last_clean or last_price_kw
        if base and add_part:
            merged = f"{base}&{add_part}"
            logger.info(
                f"[AGENT-GUARD] merged attr refinement keyword={merged!r} "
                f"from last={last_keyword!r}"
            )
            return merged
        if add_part and last_price_kw and not last_clean:
            # Prior was price-only; keep budget + new facet.
            merged = f"{add_part}&{last_price_kw}"
            logger.info(
                f"[AGENT-GUARD] merged attr+prior-price keyword={merged!r} "
                f"from last={last_keyword!r}"
            )
            return merged

    if not price_filter:
        return keyword or user_message

    if has_new_catalog_attrs and not refine_hint:
        # Fresh shop-by / mega-menu ask — do not glue previous filters.
        return user_message

    price_keyword = price_filter_to_keyword(price_filter)
    if last_clean:
        merged = f"{last_clean}&{price_keyword}"
        logger.info(
            f"[AGENT-GUARD] merged price refinement keyword={merged!r} "
            f"from last={last_keyword!r}"
        )
        return merged
    return price_keyword


def _is_price_refinement_followup(user_message: str, previous_searches) -> bool:
    if not previous_searches or not _latest_search_keyword(previous_searches):
        return False
    price_filter, _, _, _ = normalise_keyword(user_message or "")
    return price_filter is not None


def _latest_search_keyword(previous_searches) -> str:
    if not previous_searches:
        return ""
    latest = previous_searches[-1] if isinstance(previous_searches, list) else {}
    return (latest.get("keyword") or "").strip() if isinstance(latest, dict) else ""


def _shown_skus_from_searches(previous_searches, *, latest_only: bool = True) -> set[str]:
    if not previous_searches or not isinstance(previous_searches, list):
        return set()

    searches = [previous_searches[-1]] if latest_only else previous_searches
    skus: set[str] = set()
    for search in searches:
        if not isinstance(search, dict):
            continue
        for product in search.get("results", []) or []:
            if not isinstance(product, dict):
                continue
            sku = str(product.get("SKU") or product.get("sku") or "").strip().upper()
            if sku:
                skus.add(sku)
    return skus


_PRODUCT_DETAIL_PHRASES = (
    "price", "cost", "how much", "material", "made of", "fabric", "weight",
    "link", "url", "sku", "size", "dimension", "first one", "second one",
    "third one", "that rug", "this rug", "the one", "tell me more about",
    "more about", "details", "in dollars", "in usd", "in eur", "in gbp",
    "in aed", "in inr",
)

_PRODUCT_SHOW_MORE_PHRASES = {
    "show more", "show me more", "more", "more rugs", "show me more rugs",
    "show more rugs", "more like these", "show me more like these",
    "any others", "other options", "see more", "next", "any more",
    "show others", "other rugs", "more options",
}

_SEARCH_INTENT_RE = re.compile(
    r"\b(show me|show|find|search|looking for|look for|i want|i need|"
    r"new arrival|new arrivals|bestsellers?|best sellers?|"
    r"outdoor rugs?|antique rugs?|rug swatch|swatches|"
    r"under \d|above \d|below \d|over \d|\d+\s*kg|lightweight|light weight)\b",
    re.IGNORECASE,
)


def _last_assistant_message(chat_history) -> str:
    for msg in reversed(chat_history or []):
        if msg.get("role") == "assistant":
            return (msg.get("content") or "").lower()
    return ""


def _last_context_is_products(chat_history) -> bool:
    text = _last_assistant_message(chat_history)
    if not text:
        return False
    return any(
        token in text
        for token in (
            "jaipurrugs.com/in/rugs",
            "view product",
            "search more rugs",
            "**come around",
            "material:",
            "hand knotted",
            "hand tufted",
        )
    ) or (" rug" in text and "retail store" not in text)


def _last_context_is_stores(chat_history) -> bool:
    text = _last_assistant_message(chat_history)
    if not text:
        return False
    if _last_context_is_products(chat_history):
        return False
    return any(
        token in text
        for token in (
            "retail store",
            "showrooms",
            "showroom",
            "contact-us",
            "- timing:",
            "- address:",
            "i can show more stores",
        )
    )


def _is_ambiguous_show_more(user_message: str) -> bool:
    msg = (user_message or "").lower().strip()
    return msg in _STORE_FOLLOWUP_PHRASES or msg in _PRODUCT_SHOW_MORE_PHRASES


def _extract_show_more_keyword(user_message: str) -> str:
    """Parse 'show more 5x5 rugs' → '5x5'."""
    msg = (user_message or "").lower().strip()
    for prefix in ("show me more ", "show more "):
        if not msg.startswith(prefix):
            continue
        remainder = msg[len(prefix):].strip()
        for suffix in (" rugs", " rug", " carpets", " carpet", " options", " products"):
            if remainder.endswith(suffix):
                remainder = remainder[: -len(suffix)].strip()
        if remainder and remainder not in {"rugs", "rug", "more", "options", "products"}:
            return remainder
    return ""


def _shown_skus_for_keyword(previous_searches, keyword: str) -> set[str]:
    target = normalise_search_keyword(keyword)
    skus: set[str] = set()
    for search in previous_searches or []:
        if not isinstance(search, dict):
            continue
        if normalise_search_keyword(search.get("keyword") or "") != target:
            continue
        for product in search.get("results", []) or []:
            if not isinstance(product, dict):
                continue
            sku = str(product.get("SKU") or product.get("sku") or "").strip().upper()
            if sku:
                skus.add(sku)
    return skus


def _resolve_show_more_search(previous_searches, user_message: str) -> tuple[str, set[str]]:
    explicit = _extract_show_more_keyword(user_message)
    if explicit:
        return explicit, _shown_skus_for_keyword(previous_searches, explicit)
    last_keyword = _latest_search_keyword(previous_searches)
    return last_keyword, _shown_skus_from_searches(previous_searches, latest_only=True)


def _recent_chat_mentions_products(chat_history, limit: int = 8) -> bool:
    recent = format_recent_chat_for_ai(chat_history, limit=limit).lower()
    return any(
        token in recent
        for token in ("rug", "sku", "inr", "usd", "jaipurrugs.com/in/rugs", "display_price", "![")
    )


def _is_product_show_more_followup(
    user_message: str,
    chat_history,
    previous_searches=None,
) -> bool:
    msg = (user_message or "").lower().strip()
    if _extract_show_more_keyword(user_message):
        return True

    is_phrase = (
        msg in _PRODUCT_SHOW_MORE_PHRASES
        or any(
            phrase in msg
            for phrase in ("show more", "more rug", "more like", "any other", "other option", "see more")
        )
    )
    if not is_phrase:
        return False

    if _last_context_is_products(chat_history):
        return True
    if previous_searches and _latest_search_keyword(previous_searches):
        return not _last_context_is_stores(chat_history)
    return _recent_chat_mentions_products(chat_history) and not _last_context_is_stores(chat_history)


def _is_product_detail_followup(user_message: str, chat_history) -> bool:
    msg = (user_message or "").lower().strip()
    if not _recent_chat_mentions_products(chat_history):
        return False
    if _is_product_show_more_followup(user_message, chat_history):
        return False
    if _SEARCH_INTENT_RE.search(msg):
        return False
    return any(re.search(rf"\b{re.escape(phrase)}\b", msg) for phrase in _PRODUCT_DETAIL_PHRASES)


def _is_agent_handoff_query(user_message: str) -> bool:
    msg = (user_message or "").lower().strip()
    if not msg:
        return False
    cues = (
        "human agent", "talk to a human", "talk to human", "speak to a human",
        "real person", "customer care", "customer service", "connect me to",
        "connect me with", "speak to an agent", "talk to an agent",
        "transfer me", "escalate", "live agent", "live chat agent",
    )
    return any(c in msg for c in cues)


def _is_custom_rug_query(user_message: str) -> bool:
    msg = (user_message or "").lower().strip()
    if not msg:
        return False
    if "custom" not in msg and "bespoke" not in msg and "own design" not in msg:
        return False
    return any(
        t in msg
        for t in ("rug", "carpet", "design", "bespoke", "made to order", "make a rug")
    )


def _is_new_product_search_request(user_message: str, chat_history, previous_searches=None) -> bool:
    """True when the user is asking for a fresh catalog search, not a detail follow-up."""
    if _is_product_detail_followup(user_message, chat_history):
        return False
    if _is_store_query(user_message, chat_history, previous_searches):
        return False
    if _is_rug_pad_query(user_message) or _is_order_address_query(user_message):
        return False
    if _is_agent_handoff_query(user_message) or _is_custom_rug_query(user_message):
        return False
    if _is_short_affirmative(user_message):
        return False
    msg = (user_message or "").lower().strip()
    # "I want to talk…" / "I need a human…" must not match bare i want/i need alone.
    if _is_agent_handoff_query(msg):
        return False
    if _SEARCH_INTENT_RE.search(msg):
        # Still require rug/catalog signal unless it's a clear shop-by phrase.
        if any(
            t in msg
            for t in (
                "rug", "rugs", "carpet", "arrival", "bestseller", "best seller",
                "swatch", "outdoor", "antique",
            )
        ) or re.search(r"\b(under|above|below|over)\s+\d", msg):
            return True
        # Color/size-only "show me red" still counts via attrs below.
        pass
    # Bare category / attribute browses: "new arrival rugs", "wool rugs", "8x10 rugs"
    _pf, _clean, _colors, attrs = normalise_keyword(msg)
    has_search_attrs = any(
        attrs.get(key)
        for key in (
            "color", "color_exact", "shape", "size", "size_cm", "size_category",
            "material", "construction", "pattern", "room", "catalog_tag",
            "multicolor", "weight_max",
        )
    )
    rug_terms = ("rug", "rugs", "carpet", "carpets")
    if has_search_attrs and any(t in msg for t in rug_terms):
        return True
    if has_search_attrs and attrs.get("catalog_tag"):
        return True
    search_verbs = (
        "show me", "show", "find", "search", "do you have", "looking for",
        "look for", "i want", "i need", "any ",
    )
    if _is_agent_handoff_query(msg) or _is_custom_rug_query(msg):
        return False
    return any(v in msg for v in search_verbs) and any(t in msg for t in rug_terms)


_AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|please|go ahead|tell me more|"
    r"know more|more please|interested)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def _is_short_affirmative(user_message: str) -> bool:
    return bool(_AFFIRMATIVE_RE.match((user_message or "").strip()))


def _last_assistant_asks_followup(chat_history) -> bool:
    text = (_last_assistant_message(chat_history) or "").lower()
    if not text:
        return False
    cues = (
        "would you like", "want to know more", "know more", "tell you more",
        "shall i", "should i", "interested", "learn more", "more about",
        "connect you", "callback", "call you",
        "find some", "search for", "show you", "please confirm", "did you mean",
        "want me to", "shall i search", "would you like me to",
    )
    return any(c in text for c in cues)


def _prior_user_search_message(chat_history) -> str:
    """Most recent user message that looks like a catalog ask (skip yes/ok)."""
    for msg in reversed(chat_history or []):
        if msg.get("role") != "user":
            continue
        text = (msg.get("content") or "").strip()
        if not text or _is_short_affirmative(text):
            continue
        if _message_has_catalog_attrs(text) or any(
            t in text.lower() for t in ("rug", "rugs", "carpet", "dari")
        ):
            return text
    return ""


def _pinned_strategy_from_products(products) -> str:
    if not isinstance(products, list):
        return ""
    return next(
        (str(p.get("search_strategy") or "") for p in products if isinstance(p, dict) and p.get("search_strategy")),
        "",
    )


def _pinned_strategy_from_previous(previous_searches) -> str:
    if not previous_searches:
        return ""
    latest = previous_searches[-1] if isinstance(previous_searches, list) else {}
    if not isinstance(latest, dict):
        return ""
    return _pinned_strategy_from_products(latest.get("results") or [])

def agent_alert_tool(
    alert,
    sesson_id,
    callback_phone: str = "",
    callback_requested: bool = False,
    collection_name: str = "users",
):
    """Tool function to raise an agent alert"""
    try:
        channel = "whatsapp" if collection_name == "users_whatsapp" else "web"
        raise_alert(
            session_id=sesson_id,
            alert_body=alert,
            callback_phone=(callback_phone or "").strip(),
            callback_requested=bool(callback_requested or callback_phone),
            channel=channel,
        )
    except Exception:
        logger.error("Error occured while using agent alert tool call.")


_COULDNT_FIND_REPLY = (
    "Sorry, I couldn't find that. Should I connect you to a human agent for that?"
)


def _ensure_unknown_query_alert(
    session_id: str,
    user_message: str,
    reply: str,
    tools_called: list[str] | None = None,
) -> None:
    """Raise a dashboard alert when the bot could not answer (safety net)."""
    if _COULDNT_FIND_REPLY.lower() not in (reply or "").lower():
        return
    if "raise_agent_alert" in (tools_called or []):
        return
    summary = (user_message or "").strip().replace("\n", " ")[:240]
    alert = f"Bot could not answer: {summary or 'unknown question'}"
    logger.info(f"[AGENT-ALERT] auto-raising unknown-query alert session={session_id} alert={alert!r}")
    agent_alert_tool(alert=alert, sesson_id=session_id)


_STORE_KEYWORDS = {
    "store", "stores", "showroom", "showrooms", "retail", "retailer",
    "retailers", "standalone", "offline", "physical", "nearest store",
    "physical store", "visit", "in person", "see rugs", "where can i",
    "shop location", "outlet", "outlets", "gallery", "galleries",
    "directions", "timing", "timings",
}

_ORDER_ADDRESS_PHRASES = (
    "delivery address",
    "shipping address",
    "change my address",
    "change the address",
    "update my address",
    "update delivery",
    "change delivery",
    "wrong address",
    "incorrect address",
)


def _is_order_address_query(user_message: str) -> bool:
    msg_lower = (user_message or "").lower()
    return any(phrase in msg_lower for phrase in _ORDER_ADDRESS_PHRASES)


_RUG_PAD_PHRASES = (
    "rug pad",
    "rug pads",
    "anti-slip",
    "anti slip",
    "antiskid",
    "anti-skid",
    "underlay",
    "underlays",
    "slipping",
    "slip mat",
    "slip mats",
)


def _is_rug_pad_query(user_message: str) -> bool:
    msg_lower = (user_message or "").lower()
    return any(phrase in msg_lower for phrase in _RUG_PAD_PHRASES)

_STORE_FOLLOWUP_PHRASES = {
    "show more", "more", "next", "more stores", "show other stores",
    "other stores", "list more", "details", "store details",
}


def _is_store_query(user_message: str, chat_history, previous_searches=None) -> bool:
    msg_lower = (user_message or "").lower().strip()
    if _is_order_address_query(user_message):
        return False
    if "address" in msg_lower and not _is_order_address_query(user_message):
        if any(kw in msg_lower for kw in ("store", "showroom", "retail", "gallery", "outlet", "nearest")):
            return True
    if any(kw in msg_lower for kw in _STORE_KEYWORDS):
        return True

    if msg_lower in _STORE_FOLLOWUP_PHRASES or _is_ambiguous_show_more(user_message):
        if _is_product_show_more_followup(user_message, chat_history, previous_searches):
            return False
        if _last_context_is_products(chat_history):
            return False
        return _last_context_is_stores(chat_history)

    return False


def _is_store_more_followup(user_message: str) -> bool:
    return (user_message or "").lower().strip() in _STORE_FOLLOWUP_PHRASES


def _store_query_from_message(user_message: str) -> str:
    msg_lower = (user_message or "").lower()
    for store in JAIPUR_RUGS_STORE_LOCATIONS:
        city = (store.get("city") or "").lower()
        country = (store.get("country") or "").lower()
        if city and city in msg_lower:
            return city
        if country and country in msg_lower:
            return country

    aliases = {
        "bangalore": "bengaluru",
        "new delhi": "delhi",
        "delhi ncr": "delhi",
        "lower parel": "mumbai",
        "andheri": "mumbai",
        "uae": "dubai",
        "united arab emirates": "dubai",
        "saudi": "ksa",
        "saudi arabia": "ksa",
        "usa": "acworth",
        "america": "acworth",
        "coimbatore": "other cities",
        "kerala": "other cities",
        "hyderabad": "other cities",
        "milano": "milan",
    }
    for alias, city in aliases.items():
        if alias in msg_lower:
            return city

    return "all stores"


def _format_store_response(store_result: dict, limit: int = 3, offset: int = 0) -> str:
    stores = store_result.get("stores", []) if isinstance(store_result, dict) else []
    source = store_result.get("source", "") if isinstance(store_result, dict) else ""

    if not stores:
        return (
            "Sorry, I couldn't find that. Should I connect you to a human agent for that?"
        )

    selected_stores = stores[offset:offset + limit] or stores[:limit]
    intro = "Here are more Jaipur Rugs retail stores/showrooms:" if offset else "Yes, Jaipur Rugs has verified retail stores/showrooms. Here are a few:"
    lines = [intro]
    for store in selected_stores:
        lines.extend([
            "",
            f"**{store.get('name', 'Jaipur Rugs Store')}**",
            f"- Address: {store.get('address') or 'Not available in verified store data'}",
            f"- Phone: {store.get('phone') or 'Not available in verified store data'}",
            f"- Timing: {store.get('timing') or 'Not available in verified store data'}",
        ])

    if len(stores) > offset + limit:
        lines.append("")
        lines.append("I can show more stores if you want.")
    if source:
        lines.append("")
        lines.append(f"Source: {source}")

    return "\n".join(lines)


def _debug_product_rows(products) -> list[dict]:
    if not isinstance(products, list):
        return []
    rows = []
    for p in products[:3]:
        reason = p.get("recommendation_reason") or {}
        rows.append({
            "name": p.get("name", ""),
            "SKU": p.get("SKU", ""),
            "GrColor": p.get("color", ""),
            "BrColor": p.get("border_color", ""),
            "ColorFamily": p.get("color_family", ""),
            "DisplayFilter": p.get("display_filter", ""),
            "ColorMood": p.get("color_mood", ""),
            "Pattern": p.get("pattern", ""),
            "size": p.get("size", ""),
            "size_cm": p.get("size_cm", ""),
            "material": p.get("material", ""),
            "display_price": p.get("display_price", ""),
            "color_match_method": reason.get("color_match_method"),
            "color_search_tier": reason.get("color_search_tier"),
            "breakdown_matched": reason.get("breakdown_matched_percentages"),
            "why_recommended": reason.get("summary"),
        })
    return rows


def _debug_search_params(keyword: str, llm_extraction: dict | None = None) -> dict:
    """Size/price filters sent to Mongo — surfaced in browser DevTools console."""
    price_filter, clean_keyword, _, attribute_filters = normalise_keyword(keyword or "")
    params = {
        "keyword_raw": keyword or "",
        "mongo_keyword": clean_keyword,
        "sizes_ft": sorted(attribute_filters.get("size") or []),
        "sizes_cm": sorted(attribute_filters.get("size_cm") or []),
        "size_categories": sorted(attribute_filters.get("size_category") or []),
        "price_filter": price_filter,
        "extraction_source": "regex",
    }
    if (
        llm_extraction
        and llm_extraction.get("use_llm_payload")
        and llm_extraction.get("search_payload")
    ):
        payload = llm_extraction["search_payload"]
        af = payload.get("attribute_filters") or {}
        params["mongo_keyword"] = payload.get("clean_keyword") or clean_keyword
        params["sizes_ft"] = sorted(af.get("size") or [])
        params["sizes_cm"] = sorted(af.get("size_cm") or [])
        params["size_categories"] = sorted(af.get("size_category") or [])
        params["price_filter"] = payload.get("price_filter") or price_filter
        params["extraction_source"] = "llm"
    return serialise_for_json(params)


def _winning_search_strategy(products) -> str:
    if not isinstance(products, list):
        return ""
    return next(
        (str(p.get("search_strategy") or "") for p in products if p.get("search_strategy")),
        "",
    )


def _winning_fallback_note(products) -> str:
    if not isinstance(products, list):
        return ""
    return next(
        (str(p.get("fallback_note") or "") for p in products if p.get("fallback_note")),
        "",
    )


def _search_verdict(
    *,
    strategy: str,
    product_count: int,
    fallback_note: str = "",
) -> str:
    """exact | relaxed | empty — lightweight diagnose language for web debug."""
    if product_count <= 0:
        return "empty"
    s = (strategy or "").strip()
    if s.startswith("drop_") or s.startswith("relax_") or s in {"widen_price", "drop_price"}:
        return "relaxed"
    if s.startswith("mongo_drop_"):
        return "relaxed" if (fallback_note or "").strip() else "exact"
    if (fallback_note or "").strip():
        return "relaxed"
    return "exact"


def _product_search_tool_debug(
    *,
    keyword: str,
    keyword_sent_to_api: str,
    currency: str,
    products,
    llm_extract_debug: list | None = None,
    extra: dict | None = None,
) -> dict:
    llm_extraction = llm_extract_debug[0] if llm_extract_debug else None
    product_count = len(products) if isinstance(products, list) else 0
    strategy = _winning_search_strategy(products)
    fallback_note = _winning_fallback_note(products)
    size_relaxed = False
    if isinstance(products, list):
        size_relaxed = any(
            bool(p.get("size_relaxed")) for p in products if isinstance(p, dict)
        )
    verdict = _search_verdict(
        strategy=strategy,
        product_count=product_count,
        fallback_note=fallback_note,
    )
    debug = {
        "tool": "jaipur_rugs_product_search",
        "keyword": keyword_sent_to_api,
        "keyword_raw": keyword,
        "keyword_sent_to_api": keyword_sent_to_api,
        "currency": currency or "",
        "products_found": product_count,
        "strategy": strategy,
        "fallback_note": fallback_note,
        "search_verdict": verdict,
        "size_relaxed": size_relaxed,
        "products": _debug_product_rows(products),
        "search_params": _debug_search_params(keyword, llm_extraction),
    }
    if llm_extraction:
        debug["llm_extraction"] = serialise_for_json(llm_extraction)
    if extra:
        debug.update(extra)
    return debug


async def _run_product_show_more(
    *,
    user_message: str,
    session_id: str,
    chat_history,
    previous_searches,
    system_prompt: str,
    client_ip: str,
    country_code: str,
    detected_currency: str,
    collection_name: str,
    debug_collector: list | None,
) -> str | None:
    """Paginate product results for show-more follow-ups. Returns message or None."""
    if not _is_product_show_more_followup(user_message, chat_history, previous_searches):
        return None

    if previous_searches and not latest_search_has_results(previous_searches):
        last_kw = _latest_search_keyword(previous_searches)
        return (
            f"Your last search{f' ({last_kw})' if last_kw else ''} returned no rugs. "
            "Want to try a different color, size, material, or budget?"
        )

    # Prefer buffered pool from the last search (no re-query).
    # Explicit "show more 5x8" is a new size ask — do not page the old pool.
    explicit_show_more = _extract_show_more_keyword(user_message)
    buffer = get_search_buffer(session_id, collection_name=collection_name)
    buf_products = buffer.get("products") if isinstance(buffer, dict) else None
    buf_keyword = (buffer.get("keyword") or "") if isinstance(buffer, dict) else ""
    buf_offset = int(buffer.get("offset") or 0) if isinstance(buffer, dict) else 0
    buf_strategy = (buffer.get("search_strategy") or "") if isinstance(buffer, dict) else ""
    if (
        not explicit_show_more
        and isinstance(buf_products, list)
        and buf_products
        and buf_offset < len(buf_products)
    ):
        page = buf_products[buf_offset: buf_offset + SEARCH_PAGE_SIZE]
        if page:
            new_offset = buf_offset + len(page)
            save_search_buffer(
                session_id,
                buf_keyword or _latest_search_keyword(previous_searches),
                buf_products,
                offset=new_offset,
                collection_name=collection_name,
                search_strategy=buf_strategy or _pinned_strategy_from_products(buf_products),
            )
            save_previous_search(
                session_id,
                buf_keyword or _latest_search_keyword(previous_searches),
                page,
                collection_name=collection_name,
            )
            if debug_collector is not None:
                debug_collector.append(
                    _product_search_tool_debug(
                        keyword=buf_keyword,
                        keyword_sent_to_api=normalise_search_keyword(buf_keyword),
                        currency="",
                        products=page,
                        extra={
                            "follow_up": "show_more_buffer",
                            "buffer_offset": buf_offset,
                            "buffer_remaining": max(0, len(buf_products) - new_offset),
                        },
                    )
                )
            log_search_turn(
                session_id=session_id,
                search_keyword=buf_keyword,
                products_found=len(page),
                extra={"follow_up": "show_more_buffer"},
            )
            return format_product_search_message(page, more=True)

        return (
            "I've shown all rugs from that search. "
            "Want to refine by a different size, color, material, or budget?"
        )

    search_keyword, exclude_skus = _resolve_show_more_search(previous_searches, user_message)
    if not search_keyword:
        return (
            "I've shown all rugs from that search. "
            "Want to refine by a different size, color, material, or budget?"
        )

    pinned = (buffer.get("search_strategy") or "") if isinstance(buffer, dict) else ""
    if not pinned:
        pinned = _pinned_strategy_from_previous(previous_searches)
    # Explicit "show more 5x8" is a new size ask — do not pin old relax strategy.
    if _extract_show_more_keyword(user_message):
        pinned = ""

    logger.info(
        f"[AGENT-GUARD] show-more keyword={search_keyword!r} "
        f"exclude_skus={len(exclude_skus or [])} pinned_strategy={pinned!r}"
    )
    pool: list = []
    more_products = await jaipur_rugs_product_search(
        search_keyword,
        client_ip=client_ip,
        country_code=country_code,
        # Empty → resolve from keyword budget / country inside search.
        requested_currency="",
        exclude_skus=exclude_skus or None,
        user_message=user_message,
        skip_llm_extraction=True,
        pool_out=pool,
        pinned_strategy=pinned,
    )
    if isinstance(more_products, dict) and more_products.get("error"):
        return (
            "I've shown all rugs from that search. "
            "Want to refine by a different size, color, material, or budget?"
        )
    if not isinstance(more_products, list) or not more_products:
        return (
            "I've shown all rugs from that search. "
            "Want to refine by a different size, color, material, or budget?"
        )

    full_pool = pool or more_products
    save_search_buffer(
        session_id,
        search_keyword,
        full_pool,
        offset=len(more_products),
        collection_name=collection_name,
        search_strategy=_pinned_strategy_from_products(full_pool) or pinned,
    )
    save_previous_search(
        session_id,
        search_keyword,
        more_products,
        collection_name=collection_name,
    )
    if debug_collector is not None:
        debug_collector.append(
            _product_search_tool_debug(
                keyword=search_keyword,
                keyword_sent_to_api=normalise_search_keyword(search_keyword),
                currency="",
                products=more_products,
                extra={
                    "follow_up": "show_more",
                    "excluded_skus": sorted(exclude_skus),
                },
            )
        )

    return format_product_search_message(more_products, more=True)


async def chat_agent(
    chat_history,
    user_message,
    session_id,
    country_code,
    client_ip="",
    collection_name: str = "users",
    detected_currency: str = "",
    debug_collector: list = None,
    image_url: str = "",
):
    """Main Jaipur Rugs chatbot agent."""
    response = None
    try:
        logger.info(f"[AGENT-IN] session={session_id} collection={collection_name} currency={detected_currency} image={'yes' if image_url else 'no'} msg={user_message!r}")

        previous_searches = get_previous_search(
            session_id=session_id,
            collection_name=collection_name,
        )

        if not client:
            raise RuntimeError("OPENAI_API_KEY is not configured.")

        system_prompt_variable = return_system_prompt()
        if system_prompt_variable:
            system_prompt = build_system_prompt(
                system_identity=system_prompt_variable["system_identity"],
                system_conversation_style=system_prompt_variable["system_conversation_style"],
                system_product_display_format=system_prompt_variable["system_product_display_format"],
                system_others=system_prompt_variable["system_others"],
            )
        else:
            system_prompt = build_system_prompt()

        show_more_reply = await _run_product_show_more(
            user_message=user_message,
            session_id=session_id,
            chat_history=chat_history,
            previous_searches=previous_searches,
            system_prompt=system_prompt,
            client_ip=client_ip,
            country_code=country_code,
            detected_currency=detected_currency,
            collection_name=collection_name,
            debug_collector=debug_collector,
        )
        if show_more_reply:
            return show_more_reply

        # Deterministic "1st / 2nd / 3rd one" — do not invent which rug.
        # Skip when this is a fresh catalog ask ("grey rug … above 3 lakhs").
        ordinal = _resolve_product_ordinal(user_message)
        if (
            ordinal
            and not _is_new_product_search_request(
                user_message, chat_history, previous_searches
            )
            and (
                _is_product_detail_followup(user_message, chat_history)
                or re.search(
                    r"\b(1st|2nd|3rd|first|second|third|#\s*[1-3]|"
                    r"(?:option|product|number)\s*#?\s*[1-3])\b",
                    user_message or "",
                    re.I,
                )
            )
        ):
            if previous_searches and not latest_search_has_results(previous_searches):
                return (
                    "Your last search returned no rugs, so I don't have a 1st/2nd/3rd "
                    "option to open. Want to try a different color, size, or budget?"
                )
            product = _product_at_ordinal(previous_searches, ordinal)
            if product:
                return format_single_product_detail(product, ordinal=ordinal)
            return (
                f"I only have the rugs from the last search — there isn't a "
                f"#{ordinal} option to show. Ask about 1st, 2nd, or 3rd, or search again."
            )

        if (
            _is_product_detail_followup(user_message, chat_history)
            and previous_searches
            and not latest_search_has_results(previous_searches)
        ):
            last_kw = _latest_search_keyword(previous_searches)
            return (
                f"Your last search{f' ({last_kw})' if last_kw else ''} returned no rugs, "
                "so I don't have product details from that ask. "
                "Want to try a different color, size, material, or budget?"
            )

        if _is_order_address_query(user_message):
            return (
                "For delivery or shipping address changes, please contact our after-sales team at "
                "order-update@jaipurrugs.com or call +91 7665017083. Share your order number and "
                "the correct address — they will assist you."
            )

        if _is_rug_pad_query(user_message):
            support_phone = general_support_phone(country_code, session_id)
            return (
                "Yes, we sell custom anti-slip mats (rug pads) tailored to your rug size. They help "
                "with slip resistance, floor protection, cushioning, and longer rug life. "
                "Learn more: https://www.jaipurrugs.com/in/know-your-rug/about-rug-pads. "
                f"For sizing or purchase help, contact {SHOP_EMAIL} or {support_phone} "
                "(WhatsApp available)."
            )

        if _is_agent_handoff_query(user_message):
            agent_alert_tool(
                alert=f"User requested human agent: {user_message}",
                sesson_id=session_id,
                callback_requested=False,
                collection_name=collection_name,
            )
            return (
                "I've notified our team — a rug specialist will connect as soon as available. "
                "If you'd prefer a callback, share your phone number (with country code) and "
                "a preferred time."
            )

        if _is_custom_rug_query(user_message):
            return (
                "Yes, we do custom rugs — including rugs made with your own design! "
                f"Email {SHOP_EMAIL} or call +91 7665017083 with your size, materials, and "
                "any design references. Our team will guide you on lead time (typically 6–12 weeks)."
            )

        # Hard FAQ guard — never invent UPI/COD acceptance from stale KB snippets.
        _pay_msg = (user_message or "").lower()
        if re.search(
            r"\b(upi|cod|cash on delivery|net banking|paytm|phonepe|gpay|google pay)\b",
            _pay_msg,
        ) and re.search(
            r"\b(accept|accepted|payment|pay with|pay via|can i pay|do you (take|accept)|"
            r"is \w+ (ok|okay|fine)|available)\b",
            _pay_msg,
        ):
            return (
                "Per Jaipur Rugs' official FAQ, checkout lists major credit/debit cards "
                "(Visa, MasterCard, American Express) and PayPal; Snapmint financing may "
                "also appear at checkout. **UPI, COD, net banking, and wallets are not listed** "
                "as accepted payment methods. For confirmation on your order, contact "
                f"{SHOP_EMAIL} or {general_support_phone(country_code, session_id)} "
                "(WhatsApp available)."
            )

        if re.search(r"\bgift\s*cards?\b|\bgift\s*vouchers?\b", _pay_msg):
            return (
                "Yes — Jaipur Rugs offers gift cards. For denominations, purchase, or "
                "redemption help, contact "
                f"{SHOP_EMAIL} or {general_support_phone(country_code, session_id)} "
                "(WhatsApp available), or ask in chat and I can look up the latest policy details."
            )

        if _is_store_query(user_message, chat_history, previous_searches):
            store_query = _store_query_from_message(user_message)
            store_result = search_store_locations(query=store_query)
            store_count = len(store_result.get("stores", []))
            logger.info(f"[AGENT-GUARD] store query={store_query!r} -> {store_count} store(s)")
            if debug_collector is not None:
                debug_collector.append({
                    "tool": "search_store_locations",
                    "query": store_query,
                    "stores_found": store_count,
                })
            offset = 3 if _is_store_more_followup(user_message) else 0
            limit = 6 if offset else 3
            store_reply = _format_store_response(store_result, limit=limit, offset=offset)
            _ensure_unknown_query_alert(session_id, user_message, store_reply, [])
            return store_reply

        _IST = timezone(timedelta(hours=5, minutes=30))
        _now_ist = datetime.now(_IST)
        _ist_time_str = _now_ist.strftime("%A, %I:%M %p IST")
        agent_status = get_agent_status()

        input_list = [
            {"role": "developer", "content": f"Chat history:\n{format_recent_chat_for_ai(chat_history)}"},
            {"role": "developer", "content": f"Current date and time: {_ist_time_str}"},
            {"role": "developer", "content": f"Agent live status: {agent_status['label']}. Business hours: {agent_status['business_hours']}. If Offline, use this message for handoff requests: {agent_status['offline_message']}"},
            {"role": "developer", "content": f"users country code: {country_code}"},
            {"role": "developer", "content": f"User's detected local currency: {detected_currency or 'INR'}. For product search results, always show the exact `display_price` from tool output — do not convert to {detected_currency or 'INR'} unless that is already the currency in `display_price`."},
            {
                "role": "developer",
                "content": f"user name: {user_name(session_id=session_id, collection_name=collection_name)}",
            },
            {"role": "developer", "content": "Never produce filler text like 'searching...' or 'one moment please'. If a tool is needed, directly call the tool without any extra wording."},
            {
                "role": "developer",
                "content": support_contacts_blurb(country_code, session_id),
            },
            {"role": "developer", "content": "When responding: do not add any narrative, status updates, waiting messages, politeness fillers, or redundant sentences. Either answer directly or call a tool directly."},
            {"role": "developer", "content": "In greeting or welcome-style replies, ask the customer what rug size they are looking for. For follow-up size questions after products were shown, answer from Latest shown products context when possible. If the user mentions size but does not identify the product, ask which product they mean and what size they prefer."},
            {"role": "developer", "content": "For ANY question about stores, showrooms, retail locations, physical presence, address, directions, or timing — including 'do you have stores?', 'do we have stores?', 'any retail store?', 'do we have a retail store?', 'where is your nearest store?', 'nearest store', 'is there a store in [city]?', 'where can I see rugs?' — ALWAYS call `search_store_locations` first (use query 'all stores' if no city given). NEVER answer store questions from your own knowledge. Use only the data returned by the tool."},
            {"role": "developer", "content": "STORE FORMAT — when showing store results, format each store exactly like this:\n**[Store Name]**\n- Address: [full address]\n- Phone: [phone]\n- Timing: [timing]\n\nShow up to 3 stores. If more exist, offer to show more. Never combine multiple stores into a single paragraph."},
            {"role": "developer", "content": "When `jaipur_rugs_product_search` returns multiple products, include all returned products (up to 3) in the final user-visible response. Do not show only one unless only one was returned."},
            {"role": "developer", "content": "For product search results, show the exact `display_price` returned by `jaipur_rugs_product_search` in the Price line — copy it verbatim. Prefer `fabric` / MaterialDetails for composition questions. If the user asks price/size/material/weight/link for a previously shown rug, answer from Latest shown products context. For follow-up currency requests, use exact values from `mrp` only if `display_price` for that currency is not available. Do not convert between currencies yourself."},
            {"role": "developer", "content": "Only when the response contains actual rug results returned by the `jaipur_rugs_product_search` tool, append this exact line at the very end: '[🔍 Search More Rugs](https://www.jaipurrugs.com/in/search)'. Do NOT add it for cleaning, care, order, careers, custom rug, anti-slip mats/rug pads, or any non-product response."},
            {
                "role": "developer",
                "content": (
                    "Jaipur Rugs DOES sell custom anti-slip mats (rug pads/underlays). Never say otherwise. "
                    "Do NOT mention pricing. Share https://www.jaipurrugs.com/in/know-your-rug/about-rug-pads "
                    f"plus shop@jaipurrugs.com or {general_support_phone(country_code, session_id)} for purchase help."
                ),
            },
            {"role": "developer", "content": "When you cannot answer from tools or KB (and no special-topic rule applies), ALWAYS call raise_agent_alert with \"Bot could not answer: \" plus a brief summary of the user's question, then respond exactly: \"Sorry, I couldn't find that. Should I connect you to a human agent for that?\" The alert must appear on the admin dashboard for support agents."},
            {"role": "developer", "content": "If the user replies with a short affirmative (yes/sure/ok/tell me more) after you asked about an artist, collaboration, policy, or non-product topic, continue THAT topic via KB — do NOT call jaipur_rugs_product_search with a generic query."},
            {"role": "developer", "content": "Callback flow: if the user asks to be called back, ask for their phone number (with country code) if missing. When they provide a number and/or preferred time, call raise_agent_alert with callback_requested=true and callback_phone set, then confirm."},
        ]

        if image_url:
            input_list.append({
                "role": "developer",
                "content": (
                    "The user attached an image in this message — you CAN view it (provided as input_image). "
                    "Never say you cannot view attachments or images. For custom/bespoke rug requests, "
                    "describe the design you see, confirm custom rugs are available, share shop@jaipurrugs.com "
                    "and +91 7665017083 for custom-rug help, and ask for delivery location plus any missing "
                    "size or material details."
                ),
            })

        input_list.append({
            "role": "user",
            "content": (
                [
                    {"type": "input_image", "image_url": image_url},
                    {"type": "input_text", "text": user_message or "What do you see in this image?"},
                ]
                if image_url
                else user_message
            ),
        })

        if previous_searches:
            latest_keyword = _latest_search_keyword(previous_searches)
            input_list.insert(1, {
                "role": "developer",
                "content": (
                    "Latest shown products for follow-up Q&A "
                    f"(last search keyword: {latest_keyword!r}). "
                    "Use this JSON only when the user asks about a rug already listed below "
                    "(price, size, material, weight, SKU, link, or color of a shown product). "
                    "If the user starts a NEW search with different criteria, call "
                    "jaipur_rugs_product_search instead of filtering these results yourself: "
                    f"{format_recent_products_for_ai(previous_searches)}"
                ),
            })

        if _is_product_detail_followup(user_message, chat_history):
            input_list.append({
                "role": "developer",
                "content": (
                    "This message is a follow-up about previously shown products. "
                    "Answer from Latest shown products context only. "
                    "Do NOT call jaipur_rugs_product_search."
                ),
            })
        elif _is_price_refinement_followup(user_message, previous_searches):
            merged_keyword = _merge_search_with_previous("", user_message, previous_searches)
            input_list.append({
                "role": "developer",
                "content": (
                    "This refines the PREVIOUS rug search with a new budget or price range. "
                    f"You MUST call jaipur_rugs_product_search with keyword={merged_keyword!r}. "
                    "Do NOT answer from previously shown products only — run a fresh filtered search."
                ),
            })
        elif _is_new_product_search_request(user_message, chat_history, previous_searches):
            input_list.append({
                "role": "developer",
                "content": (
                    "This is a NEW product search request. You MUST call "
                    "jaipur_rugs_product_search. Build keyword ONLY from this user message "
                    f"({user_message!r}) — do NOT reuse colors, sizes, materials, rooms, "
                    "or tags from previous searches or shown products. "
                    "Do NOT answer from previously shown products or guess availability."
                ),
            })

        if _last_context_is_products(chat_history) and _is_ambiguous_show_more(user_message):
            input_list.append({
                "role": "developer",
                "content": (
                    "The user's last assistant reply showed RUG PRODUCTS. "
                    "If they said 'show more' or similar, paginate more rugs with "
                    "jaipur_rugs_product_search using the same keyword as the last search. "
                    "Do NOT call search_store_locations."
                ),
            })

        # Python-level store guard: pre-fetch store data and inject into context so the
        # LLM always has verified store data regardless of whether it calls the tool.
        _msg_lower = user_message.lower()
        if any(kw in _msg_lower for kw in _STORE_KEYWORDS):
            logger.info(f"[AGENT] Store keyword detected — pre-fetching store data")
            try:
                _store_result = search_store_locations(query="all stores")
                _store_count = len(_store_result.get("stores", []))
                logger.info(f"[AGENT] Pre-fetched {_store_count} store(s) — injecting into context")
                input_list.append({
                    "role": "developer",
                    "content": (
                        f"VERIFIED STORE DATA (already fetched — use this data directly, do NOT call search_store_locations again, do NOT answer from memory): "
                        f"{json.dumps(_store_result)}"
                    )
                })
            except Exception as _store_err:
                logger.warning(f"[AGENT] Store pre-fetch failed: {_store_err}")


        # Step 1: Model processes with tools available
        response = await client.responses.create(
            model="gpt-4.1-mini",
            tools=tools,
            input=input_list,
            temperature=0,
            instructions=system_prompt,
            max_output_tokens=2048,
            text=output_schema,
            top_p=1,
        )

        logger.info("model response", extra={"response": response})
        input_list += response.output


        # Step 2: Handle tool calls — collect ALL outputs before calling model again
        has_tool_calls = False
        tools_called: list[str] = []
        last_product_search_result = None
        last_product_search_keyword = ""
        for item in response.output:
            if item.type == "function_call":
                has_tool_calls = True
                tools_called.append(item.name)
                args = json.loads(item.arguments)
                output = ""
                logger.info(f"[AGENT-TOOL] session={session_id} tool={item.name} args={args}")

                if item.name == "jaipur_rugs_product_search":
                    model_keyword = resolve_search_keyword(args, "")
                    keyword = resolve_hygienic_search_keyword(args, user_message)
                    # Affirmative after "shall I search brown medium?" → replay prior ask.
                    if _is_short_affirmative(user_message) and (
                        _last_assistant_asks_followup(chat_history)
                        or (keyword or "").strip().lower() in {
                            "yes", "yeah", "yep", "yup", "sure", "ok", "okay",
                        }
                    ):
                        prior = _prior_user_search_message(chat_history)
                        if prior:
                            keyword = prior
                            logger.info(
                                f"[AGENT-GUARD] affirmative → replay prior search "
                                f"keyword={keyword!r}"
                            )
                    ignore_previous = _should_ignore_previous_search(keyword, user_message)
                    if not ignore_previous:
                        keyword = _merge_search_with_previous(
                            keyword,
                            user_message if not _is_short_affirmative(user_message) else keyword,
                            previous_searches,
                        )
                    exclude_skus = None
                    skip_llm_extraction = False
                    pinned_strategy = ""
                    if _is_product_show_more_followup(user_message, chat_history, previous_searches):
                        explicit_sm = _extract_show_more_keyword(user_message)
                        if explicit_sm:
                            # "show more 5x8" → new size ask, not another page of round.
                            keyword = explicit_sm
                            skip_llm_extraction = False
                            ignore_previous = True
                            pinned_strategy = ""
                            exclude_skus = _shown_skus_for_keyword(previous_searches, explicit_sm)
                        else:
                            exclude_skus = _shown_skus_from_searches(
                                previous_searches, latest_only=True
                            )
                            last_keyword = _latest_search_keyword(previous_searches)
                            if last_keyword and (
                                not keyword
                                or keyword.lower().strip() in _PRODUCT_SHOW_MORE_PHRASES
                            ):
                                keyword = last_keyword
                                skip_llm_extraction = True
                                ignore_previous = False
                            buf = get_search_buffer(session_id, collection_name=collection_name)
                            pinned_strategy = (
                                (buf.get("search_strategy") or "") if isinstance(buf, dict) else ""
                            )
                            if not pinned_strategy:
                                pinned_strategy = _pinned_strategy_from_previous(previous_searches)
                    if not keyword:
                        logger.warning(
                            f"[AGENT-TOOL] jaipur_rugs_product_search missing keyword "
                            f"— args={args!r} user_message={user_message!r}"
                        )
                    llm_extract_debug: list = []
                    pool: list = []
                    # Prefer tool currency; else let search resolve from user
                    # budget text / price_filter (do NOT force country INR over USD asks).
                    products = await jaipur_rugs_product_search(
                        keyword,
                        client_ip=client_ip,
                        country_code=country_code,
                        requested_currency=(args.get("currency") or "").strip(),
                        exclude_skus=exclude_skus,
                        user_message=(
                            keyword if _is_short_affirmative(user_message) else user_message
                        ),
                        previous_search_keyword=(
                            "" if ignore_previous else _latest_search_keyword(previous_searches)
                        ),
                        chat_context=(
                            ""
                            if ignore_previous
                            else format_recent_chat_for_ai(chat_history, limit=4)
                        ),
                        skip_llm_extraction=skip_llm_extraction,
                        extraction_debug_out=llm_extract_debug,
                        pool_out=pool,
                        pinned_strategy=pinned_strategy,
                    )
                    memory_keyword = resolved_keyword_for_memory(
                        keyword,
                        llm_extract_debug[0] if llm_extract_debug else None,
                    )
                    keyword_sent_to_api = (
                        normalise_search_keyword(memory_keyword)
                        or memory_keyword
                        or normalise_search_keyword(keyword)
                    )
                    product_list = products if isinstance(products, list) else []
                    is_error = isinstance(products, dict) and bool(products.get("error"))
                    product_count = len(product_list)
                    logger.info(
                        f"[AGENT-TOOL] jaipur_rugs_product_search "
                        f"model_kw={model_keyword!r} keyword={keyword!r} "
                        f"memory_kw={memory_keyword!r} api_keyword={keyword_sent_to_api!r} "
                        f"ignore_previous={ignore_previous} → {product_count} product(s)"
                    )
                    save_previous_search(
                        session_id,
                        memory_keyword or keyword,
                        product_list,
                        collection_name=collection_name,
                        allow_empty=is_error or product_count == 0,
                    )
                    win_strategy = _winning_search_strategy(product_list)
                    if product_list:
                        save_search_buffer(
                            session_id,
                            memory_keyword or keyword,
                            pool or product_list,
                            offset=len(product_list),
                            collection_name=collection_name,
                            search_strategy=pinned_strategy or win_strategy,
                        )
                    else:
                        clear_search_buffer(session_id, collection_name=collection_name)
                    size_relaxed = any(p.get("size_relaxed") for p in product_list)
                    win_note = _winning_fallback_note(product_list)
                    log_search_turn(
                        session_id=session_id,
                        search_keyword=memory_keyword or keyword,
                        ignore_previous=ignore_previous,
                        mongo_keyword=keyword_sent_to_api,
                        size_relaxed=size_relaxed,
                        fallback_note=win_note,
                        strategy=win_strategy,
                        products_found=product_count,
                    )
                    if debug_collector is not None:
                        debug_collector.append(
                            _product_search_tool_debug(
                                keyword=memory_keyword or keyword,
                                keyword_sent_to_api=keyword_sent_to_api,
                                currency=args.get("currency", ""),
                                products=product_list,
                                llm_extract_debug=llm_extract_debug,
                                extra={
                                    "keyword_raw_from_model": model_keyword,
                                    "ignored_previous_search": ignore_previous,
                                    "size_relaxed": size_relaxed,
                                    "strategy": win_strategy,
                                    "fallback_note": win_note,
                                    "pool_size": len(pool or product_list),
                                },
                            )
                        )
                    last_product_search_result = product_list
                    last_product_search_keyword = (
                        keyword_sent_to_api or memory_keyword or keyword or user_message
                    )
                    output = json.dumps(products if not is_error else product_list)

                elif item.name == "save_user_name":
                    name = args.get("name")
                    save_user_name(
                        session_id,
                        name,
                        collection_name=collection_name,
                    )
                    output = json.dumps({"status": "success"})

                elif item.name == "get_previous_search":
                    prev_searches = get_previous_search(
                        session_id=session_id,
                        collection_name=collection_name,
                    )
                    output = json.dumps(prev_searches)

                elif item.name == "search_kb":
                    query = args.get("query")
                    logger.info(f"[AGENT-TOOL] search_kb query={query!r}")
                    kb_hit_previews: list = []
                    kb_search_response = await fetch_similar_sessions(
                        query=query,
                        top_k=5,
                        debug_hits_out=kb_hit_previews if debug_collector is not None else None,
                    )
                    if debug_collector is not None:
                        kb_hits = 0
                        if isinstance(kb_search_response, list):
                            kb_hits = len(kb_search_response)
                        elif isinstance(kb_search_response, str) and kb_search_response.strip():
                            kb_hits = kb_search_response.count("Source:")
                        debug_collector.append({
                            "tool": "search_kb",
                            "query": query,
                            "results_found": kb_hits,
                            "hits": kb_hit_previews,
                        })
                    output = json.dumps(kb_search_response)

                elif item.name == "search_store_locations":
                    query = args.get("query")
                    result = search_store_locations(query=query)
                    store_count = len(result.get("stores", []))
                    logger.info(f"[AGENT-TOOL] search_store_locations query={query!r} → {store_count} store(s)")
                    if debug_collector is not None:
                        debug_collector.append({
                            "tool": "search_store_locations",
                            "query": query,
                            "stores_found": store_count,
                        })
                    output = json.dumps(result)

                elif item.name == "raise_agent_alert":
                    alert = args.get("alert")
                    callback_phone = (args.get("callback_phone") or "").strip()
                    callback_requested = bool(args.get("callback_requested") or callback_phone)
                    logger.info(
                        f"[AGENT-TOOL] raise_agent_alert alert={alert!r} "
                        f"callback_phone={callback_phone!r}"
                    )
                    agent_alert_tool(
                        alert=alert,
                        sesson_id=session_id,
                        callback_phone=callback_phone,
                        callback_requested=callback_requested,
                        collection_name=collection_name,
                    )
                    output = json.dumps({"status": "success"})

                input_list.append({
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": output
                })

        # Step 2b: Force product search when the model skipped the tool on a clear search request
        affirmative_replay = (
            not has_tool_calls
            and _is_short_affirmative(user_message)
            and _last_assistant_asks_followup(chat_history)
            and bool(_prior_user_search_message(chat_history))
        )
        if (
            not has_tool_calls
            and (
                affirmative_replay
                or (
                    not (_is_short_affirmative(user_message) and _last_assistant_asks_followup(chat_history))
                    and (
                        _is_new_product_search_request(
                            user_message, chat_history, previous_searches
                        )
                        or _is_price_refinement_followup(user_message, previous_searches)
                    )
                )
            )
        ):
            if affirmative_replay:
                keyword = _prior_user_search_message(chat_history)
                ignore_previous = True
            else:
                keyword = resolve_hygienic_search_keyword({}, user_message)
                ignore_previous = _should_ignore_previous_search(keyword, user_message)
                if not ignore_previous:
                    keyword = _merge_search_with_previous(
                        keyword,
                        user_message,
                        previous_searches,
                    )
            logger.info(
                f"[AGENT-FORCE-SEARCH] session={session_id} "
                f"keyword={keyword!r} ignore_previous={ignore_previous} "
                f"affirmative_replay={affirmative_replay}"
            )
            llm_extract_debug: list = []
            pool: list = []
            products = await jaipur_rugs_product_search(
                keyword,
                client_ip=client_ip,
                country_code=country_code,
                requested_currency="",
                user_message=keyword if affirmative_replay else user_message,
                previous_search_keyword=(
                    "" if ignore_previous else _latest_search_keyword(previous_searches)
                ),
                chat_context=(
                    "" if ignore_previous else format_recent_chat_for_ai(chat_history, limit=4)
                ),
                extraction_debug_out=llm_extract_debug,
                pool_out=pool,
            )
            memory_keyword = resolved_keyword_for_memory(
                keyword,
                llm_extract_debug[0] if llm_extract_debug else None,
            )
            keyword_sent_to_api = (
                normalise_search_keyword(memory_keyword)
                or memory_keyword
                or normalise_search_keyword(keyword)
            )
            product_list = products if isinstance(products, list) else []
            is_error = isinstance(products, dict) and bool(products.get("error"))
            save_previous_search(
                session_id,
                memory_keyword or keyword,
                product_list,
                collection_name=collection_name,
                allow_empty=is_error or not product_list,
            )
            win_strategy = _winning_search_strategy(product_list)
            if product_list:
                save_search_buffer(
                    session_id,
                    memory_keyword or keyword,
                    pool or product_list,
                    offset=len(product_list),
                    collection_name=collection_name,
                    search_strategy=win_strategy,
                )
            else:
                clear_search_buffer(session_id, collection_name=collection_name)
            win_note = _winning_fallback_note(product_list)
            log_search_turn(
                session_id=session_id,
                search_keyword=memory_keyword or keyword,
                ignore_previous=ignore_previous,
                mongo_keyword=keyword_sent_to_api,
                size_relaxed=any(p.get("size_relaxed") for p in product_list),
                fallback_note=win_note,
                strategy=win_strategy,
                products_found=len(product_list),
                extra={"forced": True},
            )
            if debug_collector is not None:
                debug_collector.append(
                    _product_search_tool_debug(
                        keyword=memory_keyword or keyword,
                        keyword_sent_to_api=keyword_sent_to_api,
                        currency=detected_currency,
                        products=product_list,
                        llm_extract_debug=llm_extract_debug,
                        extra={
                            "forced": True,
                            "strategy": win_strategy,
                            "fallback_note": win_note,
                        },
                    )
                )
            return format_product_search_message(
                product_list,
                no_results_keyword=keyword_sent_to_api or keyword or user_message,
            )

        if (
            has_tool_calls
            and tools_called == ["jaipur_rugs_product_search"]
            and last_product_search_result is not None
        ):
            return format_product_search_message(
                last_product_search_result if isinstance(last_product_search_result, list) else [],
                no_results_keyword=last_product_search_keyword,
            )

        # Step 3: Final model response — called ONCE after all tool outputs are collected
        if has_tool_calls:
            response = await client.responses.create(
                model="gpt-4.1-mini",
                instructions=system_prompt,
                input=input_list,
                temperature=0,
                text=output_schema
            )

        output = json.loads(response.output[0].content[0].text)
        final_message = output.get("message", "")
        # Don't rely on the model to surface relax honesty — prefix once.
        if isinstance(last_product_search_result, list) and last_product_search_result:
            final_message = ensure_search_honesty_prefix(
                final_message, last_product_search_result
            )
        _ensure_unknown_query_alert(
            session_id,
            user_message,
            final_message,
            tools_called,
        )
        logger.info(f"[AGENT-OUT] session={session_id} tools_used={has_tool_calls} reply={final_message!r}")
        return final_message

    except Exception as e:
        logger.error(
            "error occurred while generating chat response",
            extra={
                "error": str(e),
                "response": response if response else "",
                "session_id": session_id
            }
        )
        raise e
