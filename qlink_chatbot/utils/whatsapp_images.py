"""WhatsApp-safe product image URLs (Kisna-style).

WhatsApp/Gupshup often fail on raw CDN links (http, relative, webp, blocked
fetch). Normalize to HTTPS and optionally wrap via Cloudinary Fetch → JPEG.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

# Ensure .env is loaded (side effect).
from qlink_chatbot.utils import env_load  # noqa: F401
from qlink_chatbot.utils.logger_config import logger


def normalize_image_url(url: Any) -> str | None:
    """Return a usable HTTPS image URL, or None."""
    if url is None:
        return None
    text = str(url).strip()
    if not text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    if text.startswith("http://"):
        text = "https://" + text[len("http://") :]
    if not text.startswith("https://"):
        return None
    return text


def get_whatsapp_safe_image_url(raw_url: str | None) -> str | None:
    """Wrap image URLs for reliable WhatsApp delivery.

    Uses Cloudinary Fetch → JPEG when ``CLOUDINARY_CLOUD_NAME`` is set
    (same pattern as Kisna). Falls back to the normalized HTTPS URL.
    """
    normalized = normalize_image_url(raw_url)
    if not normalized:
        return None

    # Read at call time so tests / runtime env updates apply.
    cloud_name = (os.getenv("CLOUDINARY_CLOUD_NAME") or "").strip()
    if not cloud_name:
        logger.warning(
            "CLOUDINARY_CLOUD_NAME not set — sending original image URL to WhatsApp"
        )
        return normalized

    # Encode so query strings / spaces in CDN paths don't break the fetch URL.
    encoded = quote(normalized, safe="")
    cloudinary_url = (
        f"https://res.cloudinary.com/{cloud_name}"
        f"/image/fetch/f_jpg,q_85,fl_progressive/{encoded}"
    )
    logger.debug(
        "WhatsApp image wrap",
        extra={"original": normalized, "whatsapp_url": cloudinary_url},
    )
    return cloudinary_url
