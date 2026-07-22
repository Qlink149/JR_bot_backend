"""WhatsApp image hygiene + markdown stripping (Kisna-style)."""

import os
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qlink_chatbot.routes.whatsapp_routes import (
    _format_products_for_whatsapp,
    _markdown_to_whatsapp,
    _whatsapp_text_after_product_cards,
)
from qlink_chatbot.utils.whatsapp_images import (
    get_whatsapp_safe_image_url,
    normalize_image_url,
)


def test_normalize_image_url_https():
    assert normalize_image_url("http://images.jaipurrugs.com/a.jpg") == (
        "https://images.jaipurrugs.com/a.jpg"
    )
    assert normalize_image_url("//cdn.example.com/x.webp") == "https://cdn.example.com/x.webp"
    assert normalize_image_url("/relative.jpg") is None
    assert normalize_image_url("") is None


def test_cloudinary_wrap_when_configured(monkeypatch):
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "demo-cloud")
    raw = "https://images.jaipurrugs.com/prod-images/Headshot/Large/RCT_X.jpg"
    safe = get_whatsapp_safe_image_url(raw)
    assert safe.startswith("https://res.cloudinary.com/demo-cloud/image/fetch/")
    assert "f_jpg" in safe
    assert quote(raw, safe="") in safe


def test_cloudinary_fallback_without_env(monkeypatch):
    monkeypatch.delenv("CLOUDINARY_CLOUD_NAME", raising=False)
    raw = "https://images.jaipurrugs.com/prod-images/Headshot/Large/RCT_X.jpg"
    assert get_whatsapp_safe_image_url(raw) == raw


def test_markdown_strips_image_syntax():
    text = (
        "Here are some rugs:\n\n"
        "**Abrash**\n"
        "- Size: 4x6\n"
        "- ![Rug Image](https://images.jaipurrugs.com/x.jpg)\n"
    )
    out = _markdown_to_whatsapp(text)
    assert "![Rug Image]" not in out
    assert "images.jaipurrugs.com" not in out or "Rug Image" not in out
    assert "Abrash" in out


def test_format_products_uses_safe_image(monkeypatch):
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "jr-demo")
    products = [
        {
            "name": "Abrash",
            "image": "https://images.jaipurrugs.com/prod-images/Headshot/Large/A.jpg",
            "url": "https://www.jaipurrugs.com/in/rugs/a",
            "size": "4x6",
            "material": "Wool",
            "mrp": {"INR": 11970},
        }
    ]
    msgs = _format_products_for_whatsapp(products, "INR")
    assert len(msgs) == 1
    assert msgs[0]["type"] == "interactive_cta"
    assert "res.cloudinary.com/jr-demo" in msgs[0]["image_url"]
    assert "View Product" in msgs[0]["button_text"]


def test_intro_after_product_cards_avoids_url_dump():
    ai = (
        "Here are some rugs I found for you:\n\n"
        "**Abrash**\n"
        "- [🛒 View Product](https://www.jaipurrugs.com/in/rugs/a)\n"
        "- ![Rug Image](https://images.jaipurrugs.com/x.jpg)\n"
    )
    intro = _whatsapp_text_after_product_cards(ai)
    assert "http" not in intro.lower()
    assert "rugs" in intro.lower() or "found" in intro.lower()
