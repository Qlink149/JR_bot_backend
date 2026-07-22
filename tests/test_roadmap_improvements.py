"""Tests for JR improvement roadmap (trust, session, WA guards)."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qlink_chatbot.agent.chat_agent import (
    _product_at_ordinal,
    _resolve_product_ordinal,
    format_recent_products_for_ai,
)
from qlink_chatbot.routes.whatsapp_routes import _format_products_for_whatsapp
from qlink_chatbot.utils.product_format import format_product_search_message
from qlink_chatbot.utils.search_session import (
    build_search_intro,
    filter_fresh_searches,
    latest_search_has_results,
)
from qlink_chatbot.utils.whatsapp_guards import (
    is_inbound_rate_limited,
    release_phone_lock,
    try_acquire_phone_lock,
)


def test_size_relaxed_intro_disclosed():
    products = [{"name": "Abrash", "size_relaxed": True, "display_price": "INR 1"}]
    intro = build_search_intro(products=products)
    assert "exact size" in intro.lower()
    msg = format_product_search_message(products)
    assert "1. Abrash" in msg or "**1. Abrash**" in msg
    assert "exact size" in msg.lower()


def test_empty_latest_search_blocks_followup_context():
    searches = [{"keyword": "purple outdoor", "results": [], "empty": True}]
    assert latest_search_has_results(searches) is False
    payload = format_recent_products_for_ai(searches)
    assert "empty_search" in payload


def test_ordinal_resolution_and_product_lookup():
    assert _resolve_product_ordinal("tell me about the 2nd one") == 2
    assert _resolve_product_ordinal("first rug please") == 1
    searches = [{
        "keyword": "blue",
        "results": [
            {"name": "A", "SKU": "1"},
            {"name": "B", "SKU": "2"},
            {"name": "C", "SKU": "3"},
        ],
    }]
    assert _product_at_ordinal(searches, 2)["name"] == "B"
    ai = format_recent_products_for_ai(searches)
    assert '"ordinal": 1' in ai
    assert '"ordinal": 2' in ai


def test_wa_cards_use_display_price_and_ordinals(monkeypatch):
    monkeypatch.delenv("CLOUDINARY_CLOUD_NAME", raising=False)
    products = [{
        "name": "Abrash",
        "image": "https://images.jaipurrugs.com/x.jpg",
        "url": "https://www.jaipurrugs.com/in/rugs/a",
        "size": "5x8",
        "material": "Wool",
        "display_price": "INR 14,700",
        "mrp": {"INR": 999999},
        "size_relaxed": True,
    }]
    msgs = _format_products_for_whatsapp(products, "INR")
    assert msgs[0]["caption"].startswith("*1. Abrash*")
    assert "INR 14,700" in msgs[0]["caption"]
    assert "Closest available size" in msgs[0]["caption"]


def test_search_ttl_expires_stale_memory():
    stale = [{
        "keyword": "old",
        "results": [{"name": "X"}],
        "timestamp": datetime.utcnow() - timedelta(hours=5),
    }]
    assert filter_fresh_searches(stale) == []


def test_phone_lock_and_rate_limit():
    phone = "919999998877"
    release_phone_lock(phone)
    assert try_acquire_phone_lock(phone) is True
    assert try_acquire_phone_lock(phone) is False
    release_phone_lock(phone)
    assert try_acquire_phone_lock(phone) is True
    release_phone_lock(phone)

    # Flood the window
    limited = False
    for _ in range(20):
        if is_inbound_rate_limited(phone):
            limited = True
            break
    assert limited is True
