"""KB policy fixture smoke tests (no live Pinecone unless RUN_KB_INTEGRATION=1)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qlink_chatbot.database.pinecone_utils import kb_hits_preview_from_matches

POLICY_PATH = ROOT / "data" / "policy_kb_pages.json"


def _policy_blob() -> str:
    data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    return "\n".join(f"{p.get('title', '')}\n{p.get('text', '')}" for p in data.get("pages", []))


def test_policy_bundle_cod_upi_not_listed():
    blob = _policy_blob().lower()
    assert "cod" in blob
    assert "upi" in blob
    assert "not listed" in blob or "are not listed" in blob


def test_policy_bundle_india_return_window():
    blob = _policy_blob().lower()
    assert "14" in blob
    assert "return" in blob
    assert "india" in blob


def test_policy_bundle_rug_pads_sold():
    blob = _policy_blob().lower()
    assert "anti-slip" in blob or "rug pad" in blob
    assert "sells" in blob or "sell" in blob


def test_kb_hits_preview_truncates():
    matches = [
        {
            "metadata": {
                "lable": "return-policy",
                "text": "A" * 200,
            }
        }
    ]
    hits = kb_hits_preview_from_matches(matches, max_chars=120)
    assert hits[0]["label"] == "return-policy"
    assert len(hits[0]["preview"]) == 121  # 120 + ellipsis
    assert hits[0]["preview"].endswith("…")


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_KB_INTEGRATION") != "1",
    reason="Set RUN_KB_INTEGRATION=1 with Pinecone + OpenAI configured",
)
@pytest.mark.asyncio
async def test_live_kb_return_policy_hit():
    from qlink_chatbot.database.pinecone_utils import fetch_similar_sessions

    hits: list = []
    text = await fetch_similar_sessions(
        "India return exchange window days",
        top_k=3,
        debug_hits_out=hits,
    )
    assert text and "Source:" in text
    assert hits
