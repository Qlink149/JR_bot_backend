import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

load_dotenv()

from qlink_chatbot.routes.dashboard_routes import dashboard_router
from qlink_chatbot.routes.general_routes import general_router
from qlink_chatbot.routes.whatsapp_routes import whatsapp_router
from qlink_chatbot.routes.ws_routes import (
    ws_router,
)
from qlink_chatbot.utils.logger_config import logger

DEFAULT_CORS_ORIGINS = [
    "https://jaipurrugs.claraai.tech",
    "https://jaipurrugs-bot.vercel.app",
    "https://jaipurrugs-kj8bpr4k4-qlink149s-projects.vercel.app",
    "https://qlink-jr.vercel.app",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
    "http://localhost:5176",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://127.0.0.1:5175",
    "http://127.0.0.1:5176",
]


def get_cors_origins() -> list[str]:
    # Keep production frontend origins available even if Vercel env values
    # are incomplete or missing during a deployment.
    raw = os.getenv("CORS_ORIGINS", "")
    origins = [
        origin.strip()
        for origin in raw.split(",")
        if origin.strip() and origin.strip() != "*"
    ]

    for default_origin in DEFAULT_CORS_ORIGINS:
        if default_origin not in origins:
            origins.append(default_origin)

    return origins


def is_behind_proxy_cors() -> bool:
    return os.getenv("BEHIND_PROXY_CORS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_app_cors_enabled() -> bool:
    """App-level CORS is for local/dev only. Production Vultr nginx already sets CORS."""
    if is_behind_proxy_cors():
        return False
    return os.getenv("APP_CORS_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_CORS_RESPONSE_HEADERS = {
    "access-control-allow-origin",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-allow-credentials",
    "access-control-expose-headers",
    "access-control-max-age",
}


class StripProxyCorsHeadersMiddleware(BaseHTTPMiddleware):
    """Remove app CORS headers so nginx is the only layer that sets Access-Control-*."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for header in list(response.headers.keys()):
            if header.lower() in _CORS_RESPONSE_HEADERS:
                del response.headers[header]
        return response


app = FastAPI(
    title="Jaipur Rugs chatbot backend API",
    version="0.1.0",
    redoc_url=None,
    docs_url=None,
    openapi_url=None,
)

if is_app_cors_enabled():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info("[CORS] FastAPI CORSMiddleware enabled for %s", get_cors_origins())
else:
    logger.info("[CORS] FastAPI CORSMiddleware disabled (proxy/nginx handles CORS)")

if is_behind_proxy_cors():
    app.add_middleware(StripProxyCorsHeadersMiddleware)
    logger.info("[CORS] Stripping app CORS headers (nginx handles CORS)")

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
    return {
        "message": "Jaipur Rugs chatbot backend API is up and running",
        "cors": {
            "app_cors_enabled": is_app_cors_enabled(),
            "behind_proxy_cors": is_behind_proxy_cors(),
        },
    }

logger.info("Jaipur Rugs backend initialized successfully.")
