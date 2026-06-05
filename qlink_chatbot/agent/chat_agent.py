import json
import os
import re
from datetime import datetime, timedelta, timezone

from openai import AsyncOpenAI

from qlink_chatbot.agent.utils.chat_agent_prompts import build_system_prompt
from qlink_chatbot.database.mongo_utils import (
    get_previous_search,
    raise_alert,
    return_system_prompt,
    save_callback_phone,
    save_previous_search,
    save_user_name,
    user_name,
)
from qlink_chatbot.database.pinecone_utils import fetch_similar_sessions
from qlink_chatbot.utils.logger_config import logger

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
        "description": "Search Jaipur Rugs products. Extract structured filters from the user's request and pass them as separate fields — do NOT pack everything into a single keyword string.",
        "parameters": {
            "type": "object",
            "properties": {
                "colors":        {"type": "array", "items": {"type": "string"}, "description": "Color names. e.g. ['blue', 'ivory']"},
                "shapes":        {"type": "array", "items": {"type": "string"}, "description": "Rug shape. e.g. ['round'], ['runner'], ['oval']"},
                "sizes":         {"type": "array", "items": {"type": "string"}, "description": "Dimensions in WxH format. e.g. ['8x10', '5x7']"},
                "materials":     {"type": "array", "items": {"type": "string"}, "description": "Material/fabric. e.g. ['wool'], ['silk'], ['viscose']"},
                "constructions": {"type": "array", "items": {"type": "string"}, "description": "Construction type. e.g. ['hand knotted'], ['hand tufted'], ['flat weave']"},
                "styles":        {"type": "array", "items": {"type": "string"}, "description": "Design style. e.g. ['modern'], ['traditional'], ['bohemian']"},
                "price_max":     {"type": "number", "description": "Maximum price budget (in the currency field below)."},
                "currency":      {"type": "string", "description": "Currency for displaying prices AND for price_max filtering. Always set this when the user asks to see prices in a specific currency — even if there is no price budget. One of: INR, USD, EUR, GBP, AUD, CHF, SGD, AED. Defaults to the user's local currency."},
                "weight_max":    {"type": "number", "description": "Maximum weight in kg. e.g. 8 means 'under 8kg'."},
                "keyword":       {"type": "string", "description": "Free-text fallback only when none of the above fields apply. e.g. a collection name or design code."}
            },
            "required": []
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
        "description": "Perform a semantic search in the knowledge base to find related past summaries or insights from previous conversations or agent learnings.",
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
        "name": "raise_agent_alert",
        "description": "Raise an alert for a human agent to take over when the assistant cannot answer or needs support.",
        "parameters": {
            "type": "object",
            "properties": {
                "alert": {
                    "type": "string",
                    "description": "Short one-line description of why agent assistance is needed."
                }
            },
            "required": ["alert"]
        }
    },
    {
        "type": "function",
        "name": "save_callback_phone",
        "description": "Save the user's phone number after they request a callback, so agents can see it.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "The user's phone number, including country code if provided."
                }
            },
            "required": ["phone"]
        }
    },
]


_IMAGE_MD_RE = re.compile(r'!\[.*?\]\((https?://\S+?)\)')


def _user_content(text: str):
    """Return plain string for text-only messages, or a multimodal list when images are present."""
    urls = _IMAGE_MD_RE.findall(text)
    if not urls:
        return text
    clean = _IMAGE_MD_RE.sub("", text).strip()
    content = [{"type": "input_text", "text": clean or "I have shared an image."}]
    for url in urls:
        content.append({"type": "input_image", "image_url": url, "detail": "auto"})
    return content


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
    results = latest_search.get("results", []) if isinstance(latest_search, dict) else []

    if not isinstance(results, list):
        return "[]"
    compact_products = []
    for product in results[:max_products]:
        if not isinstance(product, dict):
            continue
        compact_products.append({
            "name": product.get("name", ""),
            "SKU": product.get("SKU", ""),
            "size": product.get("size", ""),
            "weight": product.get("weight", ""),
            "material": product.get("material", ""),
            "fabric": product.get("fabric", ""),
            "mrp": product.get("mrp", {}),
            "url": product.get("url", ""),
        })

    return json.dumps(compact_products)


def previously_shown_product_keys(previous_searches) -> list[str]:
    """Return SKU/BarCode keys already shown in this session."""
    keys = []
    if not isinstance(previous_searches, list):
        return keys
    for search in previous_searches:
        if not isinstance(search, dict):
            continue
        for product in search.get("results", []) or []:
            if not isinstance(product, dict):
                continue
            for field in ("SKU", "sku", "BarCode", "barcode"):
                value = product.get(field)
                if value:
                    keys.append(str(value).strip().upper())
            url = product.get("url")
            if url and "barcode=" in url:
                keys.append(str(url).rsplit("barcode=", 1)[-1].split("&", 1)[0].strip().upper())
    return list(dict.fromkeys(keys))


def previously_shown_product_names(previous_searches) -> list[str]:
    names = []
    if not isinstance(previous_searches, list):
        return names
    for search in previous_searches:
        if not isinstance(search, dict):
            continue
        for product in search.get("results", []) or []:
            if isinstance(product, dict) and product.get("name"):
                names.append(str(product["name"]).strip())
    return list(dict.fromkeys(names))


def normalize_user_message(message: str) -> str:
    text = (message or "").strip()
    text = re.sub(r"\beugs\b", "rugs", text, flags=re.IGNORECASE)
    return text


_CURRENCY_PATTERNS = (
    (r"\busd\b|\bdollars?\b", "USD"),
    (r"\binr\b|\brupees?\b|₹", "INR"),
    (r"\baed\b|\bdirhams?\b", "AED"),
    (r"\bgbp\b|\bpounds?\b", "GBP"),
    (r"\beur\b|\beuros?\b", "EUR"),
    (r"\baud\b", "AUD"),
    (r"\bchf\b", "CHF"),
    (r"\bsgd\b", "SGD"),
)


def extract_explicit_currency(message: str) -> str:
    text = (message or "").lower()
    for pattern, code in _CURRENCY_PATTERNS:
        if re.search(pattern, text):
            return code
    return ""


def is_show_more_request(message: str) -> bool:
    text = (message or "").lower()
    return bool(re.search(r"\b(show|see|view|browse|search)\s+more\b|\bmore\s+(products|rugs|options)\b", text))


def is_product_search_request(message: str) -> bool:
    text = (message or "").lower()
    if is_show_more_request(text):
        return False
    if not re.search(r"\b(show|find|search|give me|i want|i need|looking for)\b", text):
        return False
    if re.search(r"\b(rug|rugs|eugs|carpet|carpets)\b", text):
        return True
    from qlink_chatbot.utils.search_middleware import COMMON_COLORS, KNOWN_MATERIALS, _extract_colors

    colors, _ = _extract_colors(text)
    if colors:
        return True
    return any(m in text for m in KNOWN_MATERIALS)


def build_product_prefetch_filters(message: str, detected_currency: str):
    if not is_product_search_request(message):
        return None
    from qlink_chatbot.utils.search_middleware import SearchFilters, _extract_colors

    currency = (extract_explicit_currency(message) or detected_currency or "INR").upper()
    filters = SearchFilters.from_keyword(message, currency=currency)
    if not filters.has_any_filter():
        colors, _ = _extract_colors(message.lower())
        if colors:
            filters.colors = colors
        else:
            return None
    return filters


def last_product_search_filters(previous_searches) -> dict:
    if not isinstance(previous_searches, list):
        return {}
    for search in reversed(previous_searches):
        if not isinstance(search, dict):
            continue
        filters = search.get("filters")
        results = search.get("results")
        if filters and isinstance(results, list) and results:
            return filters
    return {}


def serialize_search_filters(filters) -> dict:
    return {
        "colors": list(filters.colors or []),
        "shapes": list(filters.shapes or []),
        "sizes": list(filters.sizes or []),
        "materials": list(filters.materials or []),
        "constructions": list(filters.constructions or []),
        "styles": list(filters.styles or []),
        "generics": list(filters.generics or []),
        "price_max": filters.price_filter.get("amount") if filters.price_filter else None,
        "currency": filters.price_filter.get("currency") if filters.price_filter else filters.currency,
        "weight_max": filters.weight_filter,
    }


def product_search_label(args: dict) -> str:
    parts = []
    for field in ("colors", "shapes", "sizes", "materials", "constructions", "styles"):
        values = args.get(field) or []
        if values:
            parts.extend(str(value).strip() for value in values if value)
    if args.get("price_max"):
        parts.append(f"{args.get('currency') or 'INR'} {args.get('price_max')}")
    if args.get("weight_max"):
        parts.append(f"{args.get('weight_max')}kg")
    if args.get("keyword"):
        parts.append(str(args.get("keyword")).strip())
    return " & ".join(part for part in parts if part) or "search"


def merge_keyword_filters(filters, keyword_filters) -> None:
    for attr in ("colors", "shapes", "sizes", "materials", "constructions", "styles", "generics"):
        merged = list(getattr(filters, attr, []) or [])
        for value in getattr(keyword_filters, attr, []) or []:
            if value not in merged:
                merged.append(value)
        setattr(filters, attr, merged)

    if not filters.price_filter and keyword_filters.price_filter:
        filters.price_filter = keyword_filters.price_filter
    if filters.weight_filter is None and keyword_filters.weight_filter is not None:
        filters.weight_filter = keyword_filters.weight_filter

def agent_alert_tool(alert, sesson_id):
    """Tool function to raise an agent alert"""
    try:
        raise_alert(
            session_id=sesson_id,
            alert_body=alert
        )
    except Exception:
        logger.error("Error occured while using agent alert tool call.")


async def _format_prefetched_products(
    *,
    user_message: str,
    products: list,
    detected_currency: str,
    session_id: str,
    collection_name: str,
    system_prompt: str,
    no_more: bool = False,
) -> str:
    """Single LLM call to format pre-fetched products — skips the tool round-trip."""
    display_currency = (extract_explicit_currency(user_message) or detected_currency or "INR").upper()
    if no_more:
        developer_products = (
            "No additional matching products remain. Tell the user politely and suggest "
            "broadening their search. Do NOT say you could not find rugs in a currency."
        )
    else:
        developer_products = (
            f"Products to display (show ALL {len(products)} in {display_currency}): "
            f"{json.dumps(products)}"
        )

    fast_input = [
        {"role": "developer", "content": f"user name: {user_name(session_id=session_id, collection_name=collection_name)}"},
        {"role": "developer", "content": f"Display currency: {display_currency}"},
        {"role": "developer", "content": developer_products},
        {
            "role": "developer",
            "content": (
                "Format every product with image, size, material, price, and View Product link. "
                "Use price.amount or mrp.[currency]. If price.amount is null/zero, write: "
                "'Price: Not listed in [currency] — INR: ₹[mrp.INR]'. "
                "NEVER say you could not find rugs, never ask to switch to INR, never refuse to show results. "
                "Append at the end: '[🔍 Search More Rugs](https://www.jaipurrugs.com/in/search)'"
            ),
        },
        {"role": "user", "content": _user_content(user_message)},
    ]
    response = await client.responses.create(
        model="gpt-4.1-mini",
        input=fast_input,
        instructions=system_prompt,
        max_output_tokens=2048,
        text=output_schema,
        temperature=0.2,
    )
    for out_item in (response.output or []):
        for content_item in (getattr(out_item, "content", None) or []):
            if getattr(content_item, "text", None):
                return json.loads(content_item.text).get("message")
    return "I'm sorry, I couldn't generate a response. Please try again."


async def chat_agent(
    chat_history,
    user_message,
    session_id,
    country_code,
    client_ip="",
    collection_name: str = "users",
    detected_currency: str = "",
):
    """Main Jaipur Rugs chatbot agent."""
    response = None
    try:
        if not client:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        user_message = normalize_user_message(user_message)
        detected_currency = (extract_explicit_currency(user_message) or detected_currency or "INR").upper()
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
            


        _IST = timezone(timedelta(hours=5, minutes=30))
        _now_ist = datetime.now(_IST)
        _ist_time_str = _now_ist.strftime("%A, %I:%M %p IST")

        # Fetch and format the latest shown products so the AI can answer
        # follow-up questions ("the first one", "its price in AED") accurately
        recent_searches = get_previous_search(session_id=session_id, collection_name=collection_name)
        latest_products_context = format_recent_products_for_ai(recent_searches)
        exclude_product_keys = previously_shown_product_keys(recent_searches)
        exclude_product_names = previously_shown_product_names(recent_searches)
        show_more_request = is_show_more_request(user_message)
        previous_product_filters = last_product_search_filters(recent_searches)

        show_more_prefetch: list | dict | None = None
        if show_more_request and previous_product_filters:
            from qlink_chatbot.utils.search_middleware import SearchFilters, search as _mw_search

            prefetch_currency = (
                previous_product_filters.get("currency") or detected_currency or "INR"
            ).upper()
            prefetch_filters = SearchFilters.from_params(
                colors=previous_product_filters.get("colors"),
                shapes=previous_product_filters.get("shapes"),
                sizes=previous_product_filters.get("sizes"),
                materials=previous_product_filters.get("materials"),
                constructions=previous_product_filters.get("constructions"),
                styles=previous_product_filters.get("styles"),
                generics=previous_product_filters.get("generics"),
                price_max=previous_product_filters.get("price_max"),
                currency=prefetch_currency,
                weight_max=previous_product_filters.get("weight_max"),
                exclude_keys=exclude_product_keys,
                exclude_names=exclude_product_names,
            )
            show_more_prefetch = await _mw_search(prefetch_filters, client_ip=client_ip)
            if isinstance(show_more_prefetch, list) and show_more_prefetch:
                save_previous_search(
                    session_id,
                    product_search_label(previous_product_filters),
                    show_more_prefetch,
                    collection_name=collection_name,
                    filters=serialize_search_filters(prefetch_filters),
                )

        product_prefetch: list | dict | None = None
        if not show_more_request:
            from qlink_chatbot.utils.search_middleware import search as _mw_search

            prefetch_filters = build_product_prefetch_filters(user_message, detected_currency)
            if prefetch_filters:
                prefetch_filters.exclude_keys = exclude_product_keys
                prefetch_filters.exclude_names = exclude_product_names
                product_prefetch = await _mw_search(prefetch_filters, client_ip=client_ip)
                if isinstance(product_prefetch, list) and product_prefetch:
                    save_previous_search(
                        session_id,
                        product_search_label(
                            {"colors": prefetch_filters.colors, "materials": prefetch_filters.materials,
                             "styles": prefetch_filters.styles, "currency": prefetch_filters.currency}
                        ),
                        product_prefetch,
                        collection_name=collection_name,
                        filters=serialize_search_filters(prefetch_filters),
                    )

        if isinstance(show_more_prefetch, list) and show_more_prefetch:
            return await _format_prefetched_products(
                user_message=user_message,
                products=show_more_prefetch,
                detected_currency=detected_currency,
                session_id=session_id,
                collection_name=collection_name,
                system_prompt=system_prompt,
            )
        if show_more_prefetch is not None:
            return await _format_prefetched_products(
                user_message=user_message,
                products=[],
                detected_currency=detected_currency,
                session_id=session_id,
                collection_name=collection_name,
                system_prompt=system_prompt,
                no_more=True,
            )
        if isinstance(product_prefetch, list) and product_prefetch:
            return await _format_prefetched_products(
                user_message=user_message,
                products=product_prefetch,
                detected_currency=detected_currency,
                session_id=session_id,
                collection_name=collection_name,
                system_prompt=system_prompt,
            )

        input_list = [
            {"role": "developer", "content": f"Chat history:\n{format_recent_chat_for_ai(chat_history)}"},
            {"role": "developer", "content": f"Latest shown products (use these for follow-up questions — 'the first one', 'its price', 'what material is it'): {latest_products_context}"},
            {"role": "developer", "content": f"Current date and time: {_ist_time_str}"},
            {"role": "developer", "content": f"users country code: {country_code}"},
            {"role": "developer", "content": f"User's detected local currency: {detected_currency or 'INR'}. Show product prices in this currency by default unless the user explicitly asks for a different one."},
            {"role": "developer", "content": "Any request that includes product attributes (color, size, material, construction, style, shape) OR uses words like 'show', 'find', 'search', 'give me', 'I want', 'I need' is ALWAYS a new product search — call `jaipur_rugs_product_search` immediately. Never answer such requests from previously shown products context."},
            {"role": "developer", "content": "If the user asks to see prices in a specific currency (e.g. 'show in USD', 'price in AED', 'in dollars'), set the `currency` field in the `jaipur_rugs_product_search` call to that currency code. This always triggers a new search."},
            {"role": "developer", "content": "When `jaipur_rugs_product_search` returns a list of products, you MUST display ALL of them — regardless of whether the price.amount is null or zero. For any product where price.amount is null or zero, display: 'Price: Not listed in [currency] — INR: ₹[mrp.INR]'. BANNED phrases: 'I couldn't find any rugs with [currency] pricing', 'no rugs listed with [currency] prices', 'Would you like me to search in INR instead' — these are never acceptable when the tool returned products."},
            {"role": "developer", "content": "PRICES MUST COME FROM TOOL DATA ONLY. Use the exact value from price.amount or mrp.[currency]. Never calculate, convert, estimate, or invent any price."},
            {"role": "developer", "content": "When the user asks to show more products, you MUST call `jaipur_rugs_product_search` — never repeat or re-list products already shown. The backend automatically excludes already-shown products from the new results."},
            {
                "role": "developer",
                "content": f"user name: {user_name(session_id=session_id, collection_name=collection_name)}",
            },
            {"role": "developer", "content": "Never produce filler text like 'searching...' or 'one moment please'. If a tool is needed, directly call the tool without any extra wording."},
            {"role": "developer", "content": "When responding: do not add any narrative, status updates, waiting messages, politeness fillers, or redundant sentences. Either answer directly or call a tool directly."},
            {"role": "developer", "content": "When `jaipur_rugs_product_search` returns multiple products, include all returned products (up to 3) in the final user-visible response. Do not show only one unless only one was returned."},
            {"role": "developer", "content": "Only skip the search tool if the user is asking about a SPECIFIC previously shown rug by position or name (e.g. 'what is the price of the first one?', 'the link for rug number 2', 'what material is that last one?'). In that case, answer from 'Latest shown products' using mrp.[currency] for the price. If that currency value is missing or zero, say it is not listed and give the INR price. For ALL other requests — including any request mentioning a color, size, material, style, or asking to 'show', 'find', or 'search' — call `jaipur_rugs_product_search`."},
            {"role": "developer", "content": "If the user asks to show more products or more rugs, call `jaipur_rugs_product_search` again with the same filters or search intent from the prior product request. The backend will exclude products already shown in this session."},
            {"role": "developer", "content": f"Previous product search filters for show-more requests: {json.dumps(previous_product_filters)}"},
            {"role": "developer", "content": "Only when the response contains actual rug results returned by the `jaipur_rugs_product_search` tool, append this exact line at the very end: '[🔍 Search More Rugs](https://www.jaipurrugs.com/in/search)'. Do NOT add it for cleaning, care, order, careers, custom rug, or any non-product response."},
        ]
        if show_more_prefetch is not None:
            if isinstance(show_more_prefetch, list) and show_more_prefetch:
                input_list.append({
                    "role": "developer",
                    "content": (
                        "SHOW-MORE PREFETCH RESULTS (display ALL of these NEW products only; "
                        "do NOT repeat previously shown rugs; do NOT call jaipur_rugs_product_search): "
                        f"{json.dumps(show_more_prefetch)}"
                    ),
                })
            else:
                input_list.append({
                    "role": "developer",
                    "content": (
                        "SHOW-MORE PREFETCH: no additional matching products remain. "
                        "Tell the user politely and suggest broadening their search. "
                        "Do NOT call jaipur_rugs_product_search and do NOT repeat previously shown rugs."
                    ),
                })
        input_list.append({"role": "user", "content": _user_content(user_message)})


        # Step 1: Model processes with tools available
        # Low temperature here for consistent, deterministic tool-call decisions
        response = await client.responses.create(
            model="gpt-4.1-mini",
            tools=tools,
            input=input_list,
            temperature=0.2,
            instructions=system_prompt,
            max_output_tokens=2048,
            text=output_schema,
            top_p=1,
        )

        logger.info("model response", extra={"response": response})
        input_list += response.output


        # Step 2: Handle tool calls — collect ALL outputs before calling model again
        has_tool_calls = False
        for item in response.output:
            if item.type == "function_call":
                has_tool_calls = True
                args = json.loads(item.arguments)
                output = ""
                if item.name == "jaipur_rugs_product_search":
                    from qlink_chatbot.utils.search_middleware import SearchFilters, search as _mw_search
                    kw = args.get("keyword", "")
                    resolved_currency = (args.get("currency") or detected_currency or "INR").upper()

                    if show_more_request and previous_product_filters:
                        filters = SearchFilters.from_params(
                            colors=previous_product_filters.get("colors"),
                            shapes=previous_product_filters.get("shapes"),
                            sizes=previous_product_filters.get("sizes"),
                            materials=previous_product_filters.get("materials"),
                            constructions=previous_product_filters.get("constructions"),
                            styles=previous_product_filters.get("styles"),
                            generics=previous_product_filters.get("generics"),
                            price_max=previous_product_filters.get("price_max"),
                            currency=(previous_product_filters.get("currency") or resolved_currency),
                            weight_max=previous_product_filters.get("weight_max"),
                            exclude_keys=exclude_product_keys,
                            exclude_names=exclude_product_names,
                        )
                        products = await _mw_search(filters, client_ip=client_ip)
                    elif any(args.get(f) for f in ("colors","shapes","sizes","materials","constructions","styles","price_max","weight_max")):
                        # LLM provided structured params → use middleware directly
                        filters = SearchFilters.from_params(
                            colors=args.get("colors"),
                            shapes=args.get("shapes"),
                            sizes=args.get("sizes"),
                            materials=args.get("materials"),
                            constructions=args.get("constructions"),
                            styles=args.get("styles"),
                            price_max=args.get("price_max"),
                            currency=resolved_currency,
                            weight_max=args.get("weight_max"),
                            exclude_keys=exclude_product_keys,
                            exclude_names=exclude_product_names,
                        )
                        if kw:  # merge any free-text keyword into generics
                            kw_filters = SearchFilters.from_keyword(kw, currency=resolved_currency)
                            merge_keyword_filters(filters, kw_filters)
                        products = await _mw_search(filters, client_ip=client_ip)
                    else:
                        # Fallback: LLM used old keyword-only style
                        filters = SearchFilters.from_keyword(kw, currency=resolved_currency)
                        filters.exclude_keys = exclude_product_keys
                        filters.exclude_names = exclude_product_names
                        products = await _mw_search(filters, client_ip=client_ip)
                    output = json.dumps(products)
                    if isinstance(products, list) and products:
                        search_label = product_search_label(args)
                        save_previous_search(
                            session_id,
                            search_label,
                            products,
                            collection_name=collection_name,
                            filters=serialize_search_filters(filters),
                        )

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
                    kb_search_response = await fetch_similar_sessions(query=query, top_k=5)
                    output = json.dumps(kb_search_response)

                elif item.name == "raise_agent_alert":
                    alert = args.get("alert")
                    agent_alert_tool(alert=alert, sesson_id=session_id)
                    output = json.dumps({"status": "success"})

                elif item.name == "save_callback_phone":
                    phone = args.get("phone", "")
                    save_callback_phone(session_id, phone, collection_name=collection_name)
                    output = json.dumps({"status": "saved", "phone": phone})

                input_list.append({
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": output
                })

        # Step 3: Final model response — called ONCE after all tool outputs are collected
        if has_tool_calls:
            response = await client.responses.create(
                model="gpt-4.1-mini",
                instructions=system_prompt,
                input=input_list,
                text=output_schema
            )

            logger.info("model response", extra={"response": response})

        # Find the text message output item — don't assume it's always [0]
        text_content = None
        for out_item in (response.output or []):
            try:
                for content_item in (getattr(out_item, "content", None) or []):
                    if getattr(content_item, "text", None):
                        text_content = content_item.text
                        break
            except Exception:
                pass
            if text_content:
                break

        if not text_content:
            logger.error("chat_agent: no text content found in response output",
                         extra={"response": str(response), "session_id": session_id})
            return "I'm sorry, I couldn't generate a response. Please try again."

        output = json.loads(text_content)
        return output.get("message")

    except Exception as e:
        logger.error(
            "error occurred while generating chat response",
            extra={
                "error": str(e),
                "response": response if response else "",
                "session_id": session_id
            }
        )
        return "I'm sorry, I ran into an issue processing your request. Could you please try again?"
