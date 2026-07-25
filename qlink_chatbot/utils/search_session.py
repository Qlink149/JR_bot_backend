"""Search buffer + session TTL helpers for product pagination / stale state."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from qlink_chatbot.utils.logger_config import logger

SEARCH_STATE_TTL_SECONDS = int(os.getenv("SEARCH_STATE_TTL_SECONDS", str(2 * 60 * 60)))
CHAT_HISTORY_MAX_LENGTH = int(os.getenv("CHAT_HISTORY_MAX_LENGTH", "50"))
SEARCH_POOL_SIZE = int(os.getenv("SEARCH_POOL_SIZE", "12"))
SEARCH_PAGE_SIZE = int(os.getenv("SEARCH_PAGE_SIZE", "3"))


def search_timestamp_is_stale(timestamp: Any) -> bool:
    if timestamp is None:
        return False
    try:
        if isinstance(timestamp, datetime):
            ts = timestamp
        else:
            return False
        age = datetime.utcnow() - ts.replace(tzinfo=None) if ts.tzinfo else datetime.utcnow() - ts
        return age.total_seconds() > SEARCH_STATE_TTL_SECONDS
    except Exception:
        return False


def filter_fresh_searches(previous_searches: list | None) -> list:
    """Drop entire search memory when the latest entry is older than TTL."""
    if not previous_searches or not isinstance(previous_searches, list):
        return []
    latest = previous_searches[-1] if previous_searches else None
    if isinstance(latest, dict) and search_timestamp_is_stale(latest.get("timestamp")):
        logger.info(
            "[SEARCH-SESSION] expired previous_searches (TTL=%ss)",
            SEARCH_STATE_TTL_SECONDS,
        )
        return []
    return previous_searches


def latest_search_has_results(previous_searches: list | None) -> bool:
    if not previous_searches:
        return False
    latest = previous_searches[-1] if isinstance(previous_searches, list) else {}
    if not isinstance(latest, dict):
        return False
    results = latest.get("results") or []
    return isinstance(results, list) and len(results) > 0


def products_have_size_relaxed(products: list | None) -> bool:
    for product in products or []:
        if not isinstance(product, dict):
            continue
        reason = product.get("recommendation_reason") or {}
        if product.get("size_relaxed") or reason.get("size_relaxed"):
            return True
    return False


def products_fallback_note(products: list | None) -> str:
    for product in products or []:
        if not isinstance(product, dict):
            continue
        note = (product.get("fallback_note") or "").strip()
        if note:
            return note
        reason = product.get("recommendation_reason") or {}
        note = (reason.get("fallback_note") or "").strip()
        if note:
            return note
    return ""


SIZE_RELAXED_NOTE = "No exact size match — showing closest available sizes."


def products_honesty_note(products: list | None) -> str:
    """Single honesty note (no intro base), or empty when search was exact."""
    fallback = products_fallback_note(products)
    if fallback:
        return fallback
    if products_have_size_relaxed(products):
        return SIZE_RELAXED_NOTE
    return ""


def build_search_intro(
    *,
    products: list | None,
    default: str = "Here are some rugs I found for you:",
    more: bool = False,
) -> str:
    """Honest intro when size/filter relaxations applied.

    Kisna-style: one clear note only. Prefer the progressive fallback_note;
    fall back to the size-relax line when that is the only signal.
    """
    base = "Here are more rugs I found for you:" if more else default
    note = products_honesty_note(products)
    if note:
        return f"{base}\n\n{note}"
    return base


def ensure_search_honesty_prefix(message: str, products: list | None) -> str:
    """Force one honesty note at the top of a reply (web LLM path).

    Formatter path already uses build_search_intro; this covers cases where the
    model writes the product reply and might omit fallback_note.
    """
    note = products_honesty_note(products)
    if not note:
        return message or ""
    msg = (message or "").strip()
    if not msg:
        return note
    lowered = msg.lower()
    if note.lower() in lowered:
        return msg
    # Model paraphrased the same honesty — don't stack a second note.
    if "no exact size match" in lowered and "size" in note.lower():
        return msg
    if "broadened search" in lowered and "broadened" in note.lower():
        return msg
    if "showing other shapes" in lowered and "shape" in note.lower():
        return msg
    return f"{note}\n\n{msg}"
