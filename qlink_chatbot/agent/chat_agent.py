import json
import os
from datetime import datetime, timedelta, timezone

from openai import AsyncOpenAI

from qlink_chatbot.agent.utils.chat_agent_prompts import build_system_prompt
from qlink_chatbot.database.mongo_utils import (
    get_previous_search,
    raise_alert,
    return_system_prompt,
    save_previous_search,
    save_user_name,
    user_name,
)
from qlink_chatbot.database.pinecone_utils import fetch_similar_sessions
from qlink_chatbot.utils.agent_availability import get_agent_status
from qlink_chatbot.utils.jaipur_rugs_api import jaipur_rugs_product_search
from qlink_chatbot.utils.logger_config import logger
from qlink_chatbot.utils.store_locations import search_store_locations

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
                    "description": "Single or multi-query string joined by '&'. Examples: 'red', '8x10', 'wool', 'hand knotted', 'red&8x10', 'red&8x10&USD 1000'."
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
    results = latest_search.get("results", []) if isinstance(latest_search, dict) else []

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

def agent_alert_tool(alert, sesson_id):
    """Tool function to raise an agent alert"""
    try:
        raise_alert(
            session_id=sesson_id,
            alert_body=alert
        )
    except Exception:
        logger.error("Error occured while using agent alert tool call.")


async def chat_agent(
    chat_history,
    user_message,
    session_id,
    country_code,
    client_ip="",
    collection_name: str = "users",
    detected_currency: str = "",
    debug_collector: list = None,
):
    """Main Jaipur Rugs chatbot agent."""
    response = None
    try:
        logger.info(f"[AGENT-IN] session={session_id} collection={collection_name} currency={detected_currency} msg={user_message!r}")
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
            


        _IST = timezone(timedelta(hours=5, minutes=30))
        _now_ist = datetime.now(_IST)
        _ist_time_str = _now_ist.strftime("%A, %I:%M %p IST")
        agent_status = get_agent_status()

        input_list = [
            {"role": "developer", "content": f"Chat history:\n{format_recent_chat_for_ai(chat_history)}"},
            {"role": "developer", "content": f"Current date and time: {_ist_time_str}"},
            {"role": "developer", "content": f"Agent live status: {agent_status['label']}. Business hours: {agent_status['business_hours']}. If Offline, use this message for handoff requests: {agent_status['offline_message']}"},
            {"role": "developer", "content": f"users country code: {country_code}"},
            {"role": "developer", "content": f"User's detected local currency: {detected_currency or 'INR'}. Show product prices in this currency by default unless the user explicitly asks for a different one."},
            {
                "role": "developer",
                "content": f"user name: {user_name(session_id=session_id, collection_name=collection_name)}",
            },
            {"role": "developer", "content": "Never produce filler text like 'searching...' or 'one moment please'. If a tool is needed, directly call the tool without any extra wording."},
            {"role": "developer", "content": "STRICT CONTACT RULE — only use these exact contact details, never any other: Email: shop@jaipurrugs.com | Order tracking: order-update@jaipurrugs.com | India phone: +91 8000295928 | International phone: +91 7412 060 022. Any other phone number (e.g. +91 7665017083, +91 7230005522) must NEVER be shared as a support contact — those are store numbers only."},
            {"role": "developer", "content": "When responding: do not add any narrative, status updates, waiting messages, politeness fillers, or redundant sentences. Either answer directly or call a tool directly."},
            {"role": "developer", "content": "In greeting or welcome-style replies, ask the customer what rug size they are looking for. For follow-up size questions after products were shown, answer from Latest shown products context when possible. If the user mentions size but does not identify the product, ask which product they mean and what size they prefer."},
            {"role": "developer", "content": "For ANY question about stores, showrooms, retail locations, physical presence, address, directions, or timing — including 'do you have stores?', 'do we have stores?', 'any retail store?', 'do we have a retail store?', 'where is your nearest store?', 'nearest store', 'is there a store in [city]?', 'where can I see rugs?' — ALWAYS call `search_store_locations` first (use query 'all stores' if no city given). NEVER answer store questions from your own knowledge. Use only the data returned by the tool."},
            {"role": "developer", "content": "STORE FORMAT — when showing store results, format each store exactly like this:\n**[Store Name]**\n- Address: [full address]\n- Phone: [phone]\n- Timing: [timing]\n\nShow up to 3 stores. If more exist, offer to show more. Never combine multiple stores into a single paragraph."},
            {"role": "developer", "content": "When `jaipur_rugs_product_search` returns multiple products, include all returned products (up to 3) in the final user-visible response. Do not show only one unless only one was returned."},
            {"role": "developer", "content": "For product search results, show the exact `display_price` returned by `jaipur_rugs_product_search`; do not recalculate, convert, or pick another MRP value. If the user asks price/size/material/weight/link for a previously shown rug, answer from Latest shown products context. For follow-up currency requests, use exact values from `mrp` only if `display_price` for that currency is not available. Do not convert between currencies yourself, do not estimate, and do not use exchange rates. If requested currency value is missing, clearly say it is unavailable."},
            {"role": "developer", "content": "Only when the response contains actual rug results returned by the `jaipur_rugs_product_search` tool, append this exact line at the very end: '[🔍 Search More Rugs](https://www.jaipurrugs.com/in/search)'. Do NOT add it for cleaning, care, order, careers, custom rug, or any non-product response."},
            {"role": "user", "content": user_message},
        ]

        # Python-level store guard: pre-fetch store data and inject into context so the
        # LLM always has verified store data regardless of whether it calls the tool.
        _STORE_KEYWORDS = {
            "store", "stores", "showroom", "showrooms", "retail", "nearest store",
            "physical store", "visit", "in person", "see rugs", "where can i",
            "shop location", "outlet", "gallery", "exhibition",
        }
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
        for item in response.output:
            if item.type == "function_call":
                has_tool_calls = True
                args = json.loads(item.arguments)
                output = ""
                logger.info(f"[AGENT-TOOL] session={session_id} tool={item.name} args={args}")

                if item.name == "jaipur_rugs_product_search":
                    keyword = args.get("keyword")
                    products = await jaipur_rugs_product_search(
                        keyword,
                        client_ip=client_ip,
                        country_code=country_code,
                        requested_currency=args.get("currency", ""),
                    )
                    product_count = len(products) if isinstance(products, list) else 0
                    logger.info(f"[AGENT-TOOL] jaipur_rugs_product_search keyword={keyword!r} → {product_count} product(s)")
                    save_previous_search(
                        session_id,
                        keyword,
                        products,
                        collection_name=collection_name,
                    )
                    if debug_collector is not None:
                        debug_collector.append({
                            "tool": "jaipur_rugs_product_search",
                            "keyword": keyword,
                            "currency": args.get("currency", ""),
                            "products_found": product_count,
                            "products": [
                                {
                                    "name": p.get("name", ""),
                                    "SKU": p.get("SKU", ""),
                                    "size": p.get("size", ""),
                                    "material": p.get("material", ""),
                                    "display_price": p.get("display_price", ""),
                                }
                                for p in (products[:3] if isinstance(products, list) else [])
                            ],
                        })
                    output = json.dumps(products)

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
                    kb_search_response = await fetch_similar_sessions(query=query, top_k=5)
                    if debug_collector is not None:
                        debug_collector.append({
                            "tool": "search_kb",
                            "query": query,
                            "results_found": len(kb_search_response) if isinstance(kb_search_response, list) else 0,
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
                    logger.info(f"[AGENT-TOOL] raise_agent_alert alert={alert!r}")
                    agent_alert_tool(alert=alert, sesson_id=session_id)
                    output = json.dumps({"status": "success"})

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
                temperature=0,
                text=output_schema
            )

        output = json.loads(response.output[0].content[0].text)
        final_message = output.get("message", "")
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
