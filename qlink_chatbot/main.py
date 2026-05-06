import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient

load_dotenv()

from qlink_chatbot.routes.dashboard_routes import dashboard_router
from qlink_chatbot.routes.general_routes import general_router
from qlink_chatbot.routes.whatsapp_routes import whatsapp_router
from qlink_chatbot.routes.ws_routes import (
    ws_router,
)
from qlink_chatbot.utils.logger_config import logger

app = FastAPI(
    title="Jaipur Rugs chatbot backend API",
    version="0.1.0",
    redoc_url=None,
    docs_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["JR"]
sessions_collection = db["users"]

app.include_router(dashboard_router)
app.include_router(general_router, prefix="/api/web")
app.include_router(ws_router)
app.include_router(whatsapp_router)

@app.get("/ping")
def ping():
    logger.info("Ping endpoint called")
    return {"message": "Jaipur Rugs chatbot backend API is up and running"}

logger.info("Jaipur Rugs backend initialized successfully.")
