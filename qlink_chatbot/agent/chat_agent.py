import json
import os

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
from qlink_chatbot.utils.jaipur_rugs_api import jaipur_rugs_product_search
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
        "description": "Search rugs from Jaipur Rugs API and return formatted product details like URL, weight, fabric, image, description, and MRP values.",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Single or multi-query string joined by '&'. Examples: 'red', '8x10', 'wool', 'hand knotted', 'red&8x10', 'red&8x10&USD 1000'."
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
):
    """Main Jaipur Rugs chatbot agent."""
    response = None
    try:
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
            


        input_list = [
            {"role": "developer", "content": f"Chat history:\n{format_recent_chat_for_ai(chat_history)}"},
            {"role": "developer", "content": f"users country code: {country_code}"},
            {
                "role": "developer",
                "content": f"user name: {user_name(session_id=session_id, collection_name=collection_name)}",
            },
            {"role": "developer", "content": "Never produce filler text like 'searching...' or 'one moment please'. If a tool is needed, directly call the tool without any extra wording."},
            {"role": "developer", "content": "When responding: do not add any narrative, status updates, waiting messages, politeness fillers, or redundant sentences. Either answer directly or call a tool directly."},
            {"role": "developer", "content": "When `jaipur_rugs_product_search` returns multiple products, include all returned products (up to 3) in the final user-visible response. Do not show only one unless only one was returned."},
            {"role": "developer", "content": "If the user asks price/size/material/weight/link for a previously shown rug, answer from Latest shown products context. For currency requests, use exact values from `mrp` for INR, AED, AUD, CHF, EUR, GBP, SGD, USD. Do not convert between currencies, do not estimate, and do not use exchange rates. If requested currency value is missing, clearly say it is unavailable."},
            {"role": "developer", "content": "For any product-related response (recommendations, product details, price, size, material, SKU, or link), append this exact line at the very end of the message: 'You can search more products here: https://www.jaipurrugs.com/search?k=asl-01'"},
            {"role": "user", "content": user_message}
        ]


        # Step 1: Model processes with tools available
        response = await client.responses.create(
            model="gpt-4.1-mini",
            tools=tools,
            input=input_list,
            temperature=0.7,
            instructions=system_prompt,
            max_output_tokens=2048,
            text=output_schema,
            top_p=1,
        )

        logger.info("model response", extra={"response": response})
        input_list += response.output


        # Step 2: Handle tool calls
        for item in response.output:
            if item.type == "function_call":
                args = json.loads(item.arguments)
                output = ""
                if item.name == "jaipur_rugs_product_search":
                    keyword = args.get("keyword")
                    products = await jaipur_rugs_product_search(keyword, client_ip=client_ip, country_code=country_code)
                    save_previous_search(
                        session_id,
                        keyword,
                        products,
                        collection_name=collection_name,
                    )
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
                    kb_search_response = await fetch_similar_sessions(query=query, top_k=5)
                    output = json.dumps(kb_search_response)

                elif item.name == "raise_agent_alert":
                    alert = args.get("alert")
                    agent_alert_tool(alert=alert, sesson_id=session_id)
                    output = json.dumps({"status": "success"})

                input_list.append({
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": output
                })


                # Step 3: Final model response using tool output
                response = await client.responses.create(
                    model="gpt-4.1-mini",
                    instructions=system_prompt,
                    input=input_list,
                    text=output_schema
                )

                logger.info("model response", extra={"response": response})

        output = json.loads(response.output[0].content[0].text)
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
        raise e
