"""Lightweight per-turn search / WhatsApp counters for ops."""

from __future__ import annotations

from typing import Any

from qlink_chatbot.utils.logger_config import logger


def log_search_turn(
    *,
    session_id: str = "",
    channel: str = "web",
    search_keyword: str = "",
    ignore_previous: bool = False,
    mongo_keyword: str = "",
    size_relaxed: bool = False,
    fallback_note: str = "",
    products_found: int = 0,
    wa_cards_sent: int = 0,
    dispatch_errors: int = 0,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "session_id": session_id,
        "channel": channel,
        "search_keyword": search_keyword,
        "ignore_previous": ignore_previous,
        "mongo_keyword": mongo_keyword,
        "size_relaxed": size_relaxed,
        "fallback_note": fallback_note or "",
        "products_found": products_found,
        "wa_cards_sent": wa_cards_sent,
        "dispatch_errors": dispatch_errors,
    }
    if extra:
        payload.update(extra)
    logger.info("[TRACE] search_turn", extra=payload)
