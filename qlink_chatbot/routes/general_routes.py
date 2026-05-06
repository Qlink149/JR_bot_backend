import io
import os
import time
import uuid

from docx import Document
from fastapi import APIRouter, Body, File, UploadFile, Header
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from qlink_chatbot.utils.cloudflare_client import s3
from qlink_chatbot.utils.wa.send_sarthak_img import send_template_message

from qlink_chatbot.database.mongo_utils import (
    delete_alert_by_id,
    list_all_alerts,
    return_system_prompt,
    update_system_prompt,
    agent_login
)
from qlink_chatbot.database.pinecone_utils import (
    chunk_text,
    delete_record_by_id,
    fetch_records_with_metadata,
    get_record_by_id,
    list_records_by_label,
    store_vector_summary,
)
from qlink_chatbot.utils.logger_config import logger

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["JR"]
sessions_collection = db["users"]

general_router = APIRouter()

def short_id():
    return uuid.uuid4().hex[:8]

@general_router.get("/ping")
def ping():
    logger.info("Ping endpoint called")
    return {"message": "Jaipur Rugs web backend API is up and running"}

@general_router.post("/login")
async def agent_login_route(payload: dict):
    try:
        emp_id = payload.get("emp_id", "").strip().lower()
        password = payload.get("password", "").strip()

        if not emp_id or not password:
            return JSONResponse(
                {"message": "emp_id and password are required"},
                status_code=400
            )

        result = agent_login(emp_id=emp_id, password=password)

        if not result["success"]:
            return JSONResponse({"message": result["message"]}, status_code=401)

        return JSONResponse(
            {
                "message": "Login successful",
                "data": result["data"],
            },
            status_code=200
        )

    except Exception as e:
        logger.error("Login error", extra={"error": str(e)})
        return JSONResponse(
            {"message": "Server error"},
            status_code=500
        )


@general_router.post("/toggle/{session_id}")
def toggle_ai_mode(session_id: str):
    session = sessions_collection.find_one({"session_id": session_id})
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    new_status = not session.get("is_ai", True)
    sessions_collection.update_one(
        {"session_id": session_id},
        {"$set": {"is_ai": new_status}}
    )

    return {"session_id": session_id, "is_ai": new_status}


@general_router.get("/chat_history/{session_id}")
def get_chat_history(session_id: str):
    normalized_session_id = session_id.lower()
    session = sessions_collection.find_one({"session_id": normalized_session_id}, {"_id": 0})
    if not session:
        return {"session_id": normalized_session_id, "chat_history": []}
    return {"session_id": normalized_session_id, "chat_history": session.get("chat_history", [])}


@general_router.get("/users")
def get_all_users():
    try:
        users = []
        for user in sessions_collection.find(
            {},
            {
                "session_id": 1,
                "country_code": 1,
                "user_name": 1,
                "updated_at": 1
            }
        ).sort("updated_at", -1): 
            user["_id"] = str(user["_id"])
            users.append(user)
        logger.info(f"Fetched {len(users)} users successfully (sorted by latest updated).")
        return {"total_users": len(users), "users": users}
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        return JSONResponse({"error": "Failed to fetch users"}, status_code=500)


@general_router.get("/users/{session_id}")
def get_user_by_id(session_id: str):
    try:
        user = sessions_collection.find_one({"session_id": session_id})
        if not user:
            return JSONResponse({"error": "User not found"}, status_code=404)
        user["_id"] = str(user["_id"])
        return user
    except Exception as e:
        logger.error(f"Error fetching user: {e}")
        return JSONResponse({"error": "Failed to fetch user"}, status_code=500)
    
from pydantic import BaseModel

from qlink_chatbot.agent.stock_agent import openai_stock_response


class StockRequest(BaseModel):
    stock: str

@general_router.get("/kb/all/{lable}")
def list_all_agent_kb(lable: str):
    try:
        response = list_records_by_label(lable=lable)
        if not response:
            return JSONResponse({"error": "records not found"}, status_code=404)
        return response
    except Exception as e:
        logger.error(f"Error lisiting all records: {e}")
        return JSONResponse({"error": "Failed to list records"}, status_code=500)

@general_router.post("/kb/add")
async def add_agend_record_kb(data: dict = Body(...)):
    try:
        record = data.get("record")
        lable = data.get("lable", "agent")
        await store_vector_summary(
            session_id="self",
            summary=record,
            lable=lable
        )
        logger.info(f"Stored record in kb {record}")
        return True
    except Exception as e:
        logger.error(f"Error lisiting all records: {e}")
        return JSONResponse({"error": "Failed to store records"}, status_code=500)

@general_router.put("/kb/{id}")
async def update_record_from_kb(id: str, data: dict = Body(...)):
    try:
        record = data.get("record")
        lable = data.get("lable", "agent")
        delete_record_by_id(record_id=id)
        await store_vector_summary(
            session_id="self",
            summary=record,
            lable=lable
        )
        logger.info(f"Stored record in kb {record}")
        return True
    except Exception as e:
        logger.error(f"Error updating records: {e}")
        return JSONResponse({"error": "Failed to update records"}, status_code=500)

    
@general_router.get("/kb/{id}")
def get_record_from_kb(id: str):
    try:
        response = get_record_by_id(record_id=id)
        if not response:
            return JSONResponse({"error": "record not found"}, status_code=404)
        return response
    except Exception as e:
        logger.error(f"Error lisiting all records: {e}")
        return JSONResponse({"error": "Failed to get records"}, status_code=500)
    
@general_router.post("/kb/search")
async def search_records(data: dict = Body(...)):
    try:
        query = data.get("query")
        response = await fetch_records_with_metadata(query=query, top_k=15)
        if not response:
            return JSONResponse({"error": "record not found"}, status_code=404)
        return response
    except Exception as e:
        logger.error(f"Error lisiting all records: {e}")
        return JSONResponse({"error": "Failed to get records"}, status_code=500)

@general_router.delete("/kb/{id}")
def delete_record_from_kb(id: str):
    try:
        delete_record_by_id(record_id=id)
        return True
    except Exception as e:
        logger.error(f"Error lisiting all records: {e}")
        return JSONResponse({"error": "Failed to list records"}, status_code=500)

@general_router.post("/stock")
def get_stock_data(data: StockRequest):
    try:
        response = openai_stock_response(
            input=data.stock
        )

        return JSONResponse(
            content=response,
            status_code=200
        )
    
    except Exception as e:
        logger.error(f"Error fetching stocking request: {e}")
        return JSONResponse({"error": "Failed to fetch stock request"}, status_code=500)
    

@general_router.get("/system/prompt")
def get_system_prompt():
    """Fetch the current system prompt configuration."""
    try:
        response = return_system_prompt()
        if not response:
            return JSONResponse({"error": "System prompt not found"}, status_code=404)
        logger.info("Fetched system prompt successfully.")
        return response
    except Exception as e:
        logger.error("Error fetching system prompt", extra={"error": e})
        return JSONResponse({"error": "Failed to fetch system prompt"}, status_code=500)

@general_router.put("/system/prompt")
def update_system_prompt_route(data: dict = Body(...)):
    """Update the editable fields of the system prompt.
    Expected JSON body:
    {
      "system_identity": "...",
      "system_conversation_style": "...",
      "system_product_display_format": "...",
      "system_others": "..."
    }
    """
    try:
        system_identity = data.get("system_identity")
        system_conversation_style = data.get("system_conversation_style")
        system_product_display_format = data.get("system_product_display_format")
        system_others = data.get("system_others")

        result = update_system_prompt(
            system_identity=system_identity,
            system_conversation_style=system_conversation_style,
            system_product_display_format=system_product_display_format,
            system_others=system_others
        )

        return JSONResponse(content=result, status_code=200)
    except Exception as e:
        logger.error("Error updating system prompt", extra={"error": e})
        return JSONResponse({"error": "Failed to update system prompt"}, status_code=500)
    

@general_router.post("/kb/upload-docx")
async def upload_docx_and_store(file: UploadFile = File(...)):
    """Upload a .docx file, extract text, chunk it, and store chunks in vector DB.
    """
    try:
        if not file.filename.endswith(".docx"):
            return JSONResponse({"error": "Only .docx files are supported"}, status_code=400)

        content = await file.read()
        document = Document(io.BytesIO(content))

        full_text = "\n".join([para.text.strip() for para in document.paragraphs if para.text.strip()])
        if not full_text:
            return JSONResponse({"error": "No readable text found in document"}, status_code=400)

        chunks = chunk_text(full_text, max_length=1000, overlap=100)

        for i, chunk in enumerate(chunks):
            await store_vector_summary(
                session_id="self",
                summary=chunk,
                lable="general"
            )
            logger.info(f"Stored chunk {i+1}/{len(chunks)} in vector DB")

        return {"message": f"Uploaded {file.filename}, stored {len(chunks)} chunks in vector DB"}

    except Exception as e:
        logger.error(f"Error processing docx upload: {e}")
        return JSONResponse({"error": "Failed to process and store document"}, status_code=500)


@general_router.get("/alerts/all")
async def get_all_alerts():
    """Return all the agents alerts."""
    try:
        results = list_all_alerts()
        return results if results else []
    except Exception as e:
        logger.error("Error Fetching alerts", extra={"error": e})
        return JSONResponse({"error": "Failed to fetch all agent alerts"}, status_code=500)
    
@general_router.delete("/alerts/{id}")
async def delete_alert(id: str):
    """Route to delete the agent alert by id."""
    try:
        result = delete_alert_by_id(id=id)
        if not result:
            return JSONResponse({"message": "Alert not found"}, status_code=404)
        
        JSONResponse({"message": "Record deleted successfully"}, status_code=200)
    
    except Exception:
        JSONResponse({"message": "Error occured while deleting alert"}, status_code=500)
        logger.error("Error deleting record by id")

@general_router.get("/get-upload-url")
def get_upload_url(filename: str, email: str):
    email = email.lower()
    key = f"{email}/{short_id()}"

    try:
        url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": "jr-chatbot", 
                "Key": key
            },
            ExpiresIn=60
        )

        public_url = f"https://pub-706af74a9f2443aa9e89918b8fd710b9.r2.dev/jr-chatbot/{key}"

        respone = {
            "upload_url": url,
            "final_url": public_url
        }

        # logger.error("Requst received to generate upload url", extra={"response": respone})
        return JSONResponse(respone, status_code=200)

    except Exception as e:
        logger.error("Error occured while generating upload url", extra={"error": e})

        return JSONResponse(
            {"message": "Error occured while generating upload url"}, 
            status_code=500
        )


class TriggerRequest(BaseModel):
    """Model for trigger."""
    phone_number: str

webhook_api = os.getenv("WEBHOOK_API")

@general_router.post("/wa/trigger")
async def dreamcast_webhook(
    request: TriggerRequest, x_api_key: str = Header(None)
):
    """Webhook message trigger."""
    try:
        if x_api_key != webhook_api:
            return JSONResponse(
                content={
                    "success": False,
                    "message": "Invalid or missing API Key",
                },
                status_code=401,
            )

        phone_number = request.phone_number
        logger.info("template trigger received", extra={"data": phone_number})
        send_template_message(
            phone_number=phone_number
        )

        return JSONResponse(
            content={
                "success": True
            },
            status_code=200,
        )

    except Exception as e:
        logger.exception(
            "Error in dreamcast webhook", extra={"exception": str(e)}
        )
        return JSONResponse(
            content={
                "success": False,
                "message": "Unexpected Error Occured",
                "error": str(e),
            },
            status_code=400,
        )
