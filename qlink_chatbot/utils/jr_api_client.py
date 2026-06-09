import os
import time

import httpx

from qlink_chatbot.utils.logger_config import logger

_BASE = "https://webapi.jaipurrugs.com/api"

_CREDS = {
    "username": os.getenv("JR_API_USERNAME", ""),
    "password": os.getenv("JR_API_PASSWORD", ""),
    "client_id": os.getenv("JR_API_CLIENT_ID", ""),
    "client_secret": os.getenv("JR_API_CLIENT_SECRET", ""),
    "grant_type": "password",
}

_token_cache: dict = {"token": "", "expires_at": 0.0}


async def _get_token(force_refresh: bool = False) -> str:
    """Return a valid bearer token, refreshing if expired, missing, or forced."""
    now = time.time()
    if not force_refresh and _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{_BASE}/oauth/access_token", json=_CREDS)
        resp.raise_for_status()
        data = resp.json()

    token = data["token"]
    # API doc spells the key "expirexpires_in" — handle both spellings
    expires_ms = int(data.get("expirexpires_in") or data.get("expires_in") or 900_000)
    _token_cache["token"] = token
    # Refresh at 85% of TTL to avoid edge-case expiry during a slow request
    _token_cache["expires_at"] = time.time() + (expires_ms / 1_000) * 0.85
    logger.info("JR API: bearer token refreshed")
    return token


async def search_products(keyword: str) -> list[dict]:
    """POST to product-master-search and return raw product list.

    Retries once with a fresh token on 401 (handles server-side token invalidation).
    """
    for attempt in range(2):
        token = await _get_token(force_refresh=(attempt > 0))
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_BASE}/WebsiteMaster/product-master-search",
                json={"searchKeyword": keyword},
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 401 and attempt == 0:
            _token_cache["token"] = ""
            logger.warning("JR API: 401 on product search — retrying with fresh token")
            continue
        resp.raise_for_status()
        return resp.json() or []
    return []


async def get_all_products() -> list[dict]:
    """Fetch the full product master (use for admin sync, not per-query search).

    Retries once with a fresh token on 401.
    """
    for attempt in range(2):
        token = await _get_token(force_refresh=(attempt > 0))
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{_BASE}/WebsiteMaster/product-master",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 401 and attempt == 0:
            _token_cache["token"] = ""
            logger.warning("JR API: 401 on product master — retrying with fresh token")
            continue
        resp.raise_for_status()
        return resp.json() or []
    return []
