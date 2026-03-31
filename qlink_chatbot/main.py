import os

from fastapi import FastAPI
from pymongo import MongoClient

from qlink_chatbot.routes.general_routes import general_router
from qlink_chatbot.routes.whatsapp_routes import whatsapp_router
from qlink_chatbot.routes.ws_routes import (
    ws_router,
)
from qlink_chatbot.utils.logger_config import logger

app = FastAPI(
    title="Qlink JR backend API",
    version="0.1.0",
    redoc_url=None,
    docs_url=None,
    openapi_url=None,
)

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["JR"]
sessions_collection = db["users"]

app.include_router(general_router, prefix="/api/web")
app.include_router(ws_router)
app.include_router(whatsapp_router)

@app.get("/ping")
def ping():
    logger.info("Ping endpoint called")
    return {"message": "Qlink <> Jaipur Rugs backend API is up and running"}

logger.info("Qlink JR backend initialized successfully.")
