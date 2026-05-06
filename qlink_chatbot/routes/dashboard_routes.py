import json
from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from qlink_chatbot.agent.utils.chat_agent_prompts import (
    system_conversation_style,
    system_identity,
    system_others,
    system_product_display_format,
)
from qlink_chatbot.database.mongo_utils import (
    internal_collection,
    whatsapp_sessions_collection,
)
from qlink_chatbot.utils.env_load import gupshup_source
from qlink_chatbot.utils.jaipur_rugs_api import products_collection as website_products_collection
from qlink_chatbot.whatsapp_functions.dispatch import dispatch_whatsapp_responses

dashboard_router = APIRouter(prefix="/api")

manual_products_collection = whatsapp_sessions_collection.database["dashboard_products"]
catalog_designs_collection = whatsapp_sessions_collection.database["catalog_designs"]


def _jsonable(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _message_type(content: str) -> str:
    lower = (content or "").lower()
    if lower.startswith("[image]"):
        return "image"
    if lower.startswith("[list]"):
        return "list"
    if lower.startswith("[buttons]"):
        return "buttons"
    if lower.startswith("[document]") or lower.startswith("[document-fallback]"):
        return "document"
    if lower.startswith("[template]"):
        return "template"
    return "text"


def _session_to_conversation(session: dict) -> dict:
    history = session.get("chat_history") or []
    last_message = history[-1] if history else {}
    return {
        "phone": session.get("session_id", ""),
        "name": session.get("user_name") or "Customer",
        "last_message": last_message.get("content", ""),
        "last_message_at": _jsonable(last_message.get("timestamp") or session.get("updated_at")),
        "is_ai": session.get("is_ai", True),
    }


def _history_to_messages(history: list[dict]) -> list[dict]:
    messages = []
    for item in history or []:
        role = item.get("role", "")
        content = item.get("content", "")
        messages.append(
            {
                "direction": "inbound" if role == "user" else "outbound",
                "role": role,
                "content": content,
                "message_type": _message_type(content),
                "timestamp": _jsonable(item.get("timestamp")),
            }
        )
    return messages


@dashboard_router.get("/stats")
def get_stats():
    sessions = list(whatsapp_sessions_collection.find({}, {"chat_history": 1}))
    inbound = 0
    outbound = 0
    for session in sessions:
        for message in session.get("chat_history") or []:
            if message.get("role") == "user":
                inbound += 1
            else:
                outbound += 1

    return {
        "total_users": len(sessions),
        "total_leads": len(sessions),
        "total_messages": inbound + outbound,
        "inbound_messages": inbound,
        "outbound_messages": outbound,
        "whatsapp_number": gupshup_source,
    }


@dashboard_router.get("/conversations")
def get_conversations():
    sessions = whatsapp_sessions_collection.find({}).sort("updated_at", -1)
    return [_session_to_conversation(session) for session in sessions]


@dashboard_router.get("/conversations/{phone}")
def get_conversation(phone: str):
    session = whatsapp_sessions_collection.find_one({"session_id": phone.lower()})
    if not session:
        return {"phone": phone, "messages": []}
    return {"phone": phone, "messages": _history_to_messages(session.get("chat_history", []))}


@dashboard_router.get("/leads")
def get_leads():
    sessions = whatsapp_sessions_collection.find({}).sort("updated_at", -1)
    leads = []
    for session in sessions:
        leads.append(
            {
                "id": str(session.get("_id")),
                "phone": session.get("session_id", ""),
                "name": session.get("user_name") or "Customer",
                "status": "active" if session.get("is_ai", True) else "agent",
                "lead_type": "WhatsApp",
                "created_at": _jsonable(session.get("created_at") or session.get("updated_at")),
                "requirement": "",
                "location": session.get("country_code", ""),
            }
        )
    return {"data": leads}


@dashboard_router.post("/whatsapp/send")
def send_whatsapp_message(payload: dict = Body(...)):
    phone = (payload.get("phone") or "").strip()
    message = (payload.get("message") or "").strip()
    if not phone or not message:
        raise HTTPException(status_code=400, detail="phone and message are required")

    dispatch_whatsapp_responses(phone_number=phone, bot_responses=[{"type": "text", "text": message}])
    whatsapp_sessions_collection.update_one(
        {"session_id": phone.lower()},
        {
            "$push": {
                "chat_history": {
                    "role": "assistant",
                    "content": message,
                    "timestamp": datetime.utcnow(),
                }
            },
            "$set": {"updated_at": datetime.utcnow()},
            "$setOnInsert": {
                "created_at": datetime.utcnow(),
                "country_code": "",
                "is_ai": True,
                "user_name": "Customer",
            },
        },
        upsert=True,
    )
    return {"success": True}


@dashboard_router.get("/prompt")
def get_prompt():
    doc = internal_collection.find_one({"category": "system_prompt"}, {"_id": 0}) or {}
    prompt = doc.get("system_identity") or system_identity
    return {"prompt": prompt.strip()}


@dashboard_router.post("/prompt")
def save_prompt(payload: dict = Body(...)):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    internal_collection.update_one(
        {"category": "system_prompt"},
        {
            "$set": {
                "category": "system_prompt",
                "system_identity": prompt,
                "system_conversation_style": system_conversation_style,
                "system_product_display_format": system_product_display_format,
                "system_others": system_others,
            }
        },
        upsert=True,
    )
    return {"success": True}


def _product_doc(product: dict) -> dict:
    product = _jsonable(product)
    product["id"] = product.pop("_id", product.get("id"))
    product["source"] = product.get("source", "manual")
    return product


def _website_product_doc(product: dict) -> dict:
    raw = product.get("raw") or {}
    search = product.get("search") or {}
    material = search.get("material") or {}
    size = search.get("size") or {}
    barcode = raw.get("BarCode") or product.get("BarCode") or raw.get("SKU") or ""
    slug = raw.get("ProductURL") or ""
    name = raw.get("Name") or raw.get("Design") or raw.get("SKU") or "Jaipur Rugs Product"
    category = raw.get("Collection") or search.get("style") or ""
    image_urls = [
        raw.get("HeadShot"),
        raw.get("Corner"),
        raw.get("CloseUp"),
        raw.get("Floorshot"),
    ]
    image_urls = [url for url in image_urls if url and not url.endswith("/")]

    return {
        "id": f"jr:{barcode or slug}",
        "source": "jaipur_rugs",
        "name": name,
        "type": raw.get("ProductType") or "Rugs",
        "category": category,
        "collection": category,
        "price": raw.get("INR_MRP") or search.get("price") or "",
        "buy_link": (
            f"https://www.jaipurrugs.com/in/rugs/{slug}?barcode={barcode}"
            if slug
            else ""
        ),
        "description": raw.get("FullDescription") or raw.get("ShortDescription") or "",
        "location": "",
        "image_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls,
        "baseTags": " | ".join(
            str(value)
            for value in [
                category,
                search.get("style"),
                search.get("construction"),
                material.get("primary"),
                size.get("group"),
            ]
            if value
        ),
        "sku": raw.get("SKU") or "",
        "barcode": barcode,
    }


@dashboard_router.get("/products")
def list_products(skip: int = 0, limit: int = 100, q: str = ""):
    manual_query = {}
    website_query = {"flags.inStock": True}
    if q:
        manual_query = {
            "$or": [
                {"name": {"$regex": q, "$options": "i"}},
                {"type": {"$regex": q, "$options": "i"}},
                {"category": {"$regex": q, "$options": "i"}},
                {"description": {"$regex": q, "$options": "i"}},
            ]
        }
        website_query["$or"] = [
            {"raw.Name": {"$regex": q, "$options": "i"}},
            {"raw.Collection": {"$regex": q, "$options": "i"}},
            {"raw.Design": {"$regex": q, "$options": "i"}},
            {"raw.SKU": {"$regex": q, "$options": "i"}},
            {"raw.BarCode": {"$regex": q, "$options": "i"}},
            {"raw.FullDescription": {"$regex": q, "$options": "i"}},
            {"search.style": {"$regex": q, "$options": "i"}},
            {"search.material.primary": {"$regex": q, "$options": "i"}},
        ]

    manual_total = manual_products_collection.count_documents(manual_query)
    website_total = website_products_collection.count_documents(website_query)

    data = []
    remaining = limit
    website_skip = max(0, skip - manual_total)

    if skip < manual_total and remaining > 0:
        manual_products = (
            manual_products_collection.find(manual_query)
            .sort("updated_at", -1)
            .skip(skip)
            .limit(remaining)
        )
        manual_data = [_product_doc(product) for product in manual_products]
        data.extend(manual_data)
        remaining -= len(manual_data)

    if remaining > 0:
        website_products = (
            website_products_collection.find(website_query, {"_id": 0})
            .sort("raw.ModifyDate", -1)
            .skip(website_skip)
            .limit(remaining)
        )
        data.extend([_website_product_doc(product) for product in website_products])

    return {"data": data, "meta": {"total": manual_total + website_total}}


@dashboard_router.post("/products")
async def create_product(
    name: str = Form(...),
    type: str = Form(""),
    category: str = Form(""),
    price: str = Form(""),
    buy_link: str = Form(""),
    description: str = Form(""),
    location: str = Form(""),
    image: UploadFile | None = File(None),
):
    now = datetime.utcnow()
    doc = {
        "name": name,
        "type": type,
        "category": category,
        "price": price,
        "buy_link": buy_link,
        "description": description,
        "location": location,
        "image_url": "",
        "image_filename": image.filename if image else "",
        "created_at": now,
        "updated_at": now,
    }
    result = manual_products_collection.insert_one(doc)
    doc["_id"] = result.inserted_id
    return {"data": _product_doc(doc)}


@dashboard_router.patch("/products/{product_id}")
async def update_product(
    product_id: str,
    name: str = Form(...),
    type: str = Form(""),
    category: str = Form(""),
    price: str = Form(""),
    buy_link: str = Form(""),
    description: str = Form(""),
    location: str = Form(""),
    image: UploadFile | None = File(None),
):
    if product_id.startswith("jr:"):
        raise HTTPException(
            status_code=400,
            detail="Website products are read-only. Edit them in the Jaipur Rugs source catalog.",
        )

    update = {
        "name": name,
        "type": type,
        "category": category,
        "price": price,
        "buy_link": buy_link,
        "description": description,
        "location": location,
        "updated_at": datetime.utcnow(),
    }
    if image:
        update["image_filename"] = image.filename
    manual_products_collection.update_one({"_id": ObjectId(product_id)}, {"$set": update})
    product = manual_products_collection.find_one({"_id": ObjectId(product_id)})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"data": _product_doc(product)}


@dashboard_router.delete("/products/{product_id}")
def delete_product(product_id: str):
    if product_id.startswith("jr:"):
        raise HTTPException(
            status_code=400,
            detail="Website products are read-only. Remove them from the Jaipur Rugs source catalog.",
        )
    result = manual_products_collection.delete_one({"_id": ObjectId(product_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"success": True}


@dashboard_router.post("/catalog/upload")
async def upload_catalog(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON catalog") from exc

    items = data if isinstance(data, list) else data.get("designs") or data.get("products") or []
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="Catalog JSON must contain a list")

    catalog_designs_collection.delete_many({})
    docs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        docs.append(
            {
                "designId": str(item.get("designId") or item.get("id") or ObjectId()),
                "name": item.get("name") or item.get("designName") or item.get("title") or "Untitled",
                "category": item.get("category") or item.get("collection") or "",
                "description": item.get("description") or "",
                "image_url": item.get("image_url") or item.get("image") or "",
                "buy_link": item.get("buy_link") or item.get("url") or "",
                "raw": item,
                "updated_at": datetime.utcnow(),
            }
        )
    if docs:
        catalog_designs_collection.insert_many(docs)
    return {"designs": len(docs)}


@dashboard_router.get("/catalog/search")
def search_catalog(q: str, limit: int = 5):
    query = {
        "$or": [
            {"name": {"$regex": q, "$options": "i"}},
            {"category": {"$regex": q, "$options": "i"}},
            {"description": {"$regex": q, "$options": "i"}},
        ]
    }
    results = [_jsonable(item) for item in catalog_designs_collection.find(query).limit(limit)]
    return {
        "results": results,
        "catalog_reply": {
            "type": "design" if results else "no_match",
            "reply_text": results[0]["name"] if results else "No catalog match found.",
            "images": [item.get("image_url") for item in results if item.get("image_url")],
            "documents": [],
        },
        "source_tree_matches": results,
    }


@dashboard_router.put("/catalog/designs/{design_id}/recommendation")
def save_catalog_recommendation(design_id: str, payload: dict = Body(...)):
    result = catalog_designs_collection.update_one(
        {"designId": design_id},
        {"$set": {"description": payload.get("description", ""), "updated_at": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Catalog design not found")
    return {"success": True}


@dashboard_router.delete("/catalog/designs/{design_id}/recommendation")
def delete_catalog_recommendation(design_id: str):
    catalog_designs_collection.update_one(
        {"designId": design_id},
        {"$set": {"description": "", "updated_at": datetime.utcnow()}},
    )
    return {"success": True}
