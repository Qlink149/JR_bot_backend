"""Per-phone WhatsApp concurrency lock + inbound rate limit (Kisna-style)."""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from qlink_chatbot.utils.logger_config import logger

INBOUND_RATE_LIMIT = int(os.getenv("WHATSAPP_INBOUND_RATE_LIMIT", "10"))
INBOUND_RATE_WINDOW = float(os.getenv("WHATSAPP_INBOUND_RATE_WINDOW", "60"))
LOCK_STALE_SECONDS = float(os.getenv("WHATSAPP_PHONE_LOCK_STALE_SECONDS", "120"))

_inbound_counts: dict[str, deque] = {}
_phone_locks: dict[str, float] = {}
_guard_lock = threading.Lock()


def is_inbound_rate_limited(phone: str) -> bool:
    """Return True if this phone already hit the inbound window limit."""
    key = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not key:
        return False
    now = time.time()
    with _guard_lock:
        window = _inbound_counts.setdefault(key, deque())
        while window and now - window[0] > INBOUND_RATE_WINDOW:
            window.popleft()
        if len(window) >= INBOUND_RATE_LIMIT:
            logger.warning(
                "WhatsApp inbound rate limited",
                extra={"phone": key, "limit": INBOUND_RATE_LIMIT},
            )
            return True
        window.append(now)
        return False


def try_acquire_phone_lock(phone: str) -> bool:
    """Acquire a short-lived per-phone processing lock. False if already busy."""
    key = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not key:
        return True
    now = time.time()
    with _guard_lock:
        held_at = _phone_locks.get(key)
        if held_at is not None and now - held_at < LOCK_STALE_SECONDS:
            logger.warning(
                "WhatsApp phone lock busy — skipping overlapping process",
                extra={"phone": key},
            )
            return False
        _phone_locks[key] = now
        return True


def release_phone_lock(phone: str) -> None:
    key = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not key:
        return
    with _guard_lock:
        _phone_locks.pop(key, None)
