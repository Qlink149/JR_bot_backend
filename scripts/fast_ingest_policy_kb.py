#!/usr/bin/env python3
"""Fast ingest of pre-fetched Jaipur Rugs policy/FAQ pages into Pinecone (no browser)."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from qlink_chatbot.database.pinecone_utils import (  # noqa: E402
    chunk_text,
    delete_records_by_prefix,
    get_embedding,
    upsert_kb,
)

POLICY_PREFIX = "policy_"
BUNDLE_PATH = ROOT / "data" / "policy_kb_pages.json"


def _doc_id(url: str, chunk_index: int) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"{POLICY_PREFIX}{digest}_{chunk_index}"


def _page_to_chunks(page: dict) -> list[str]:
    url = page.get("url", "")
    title = page.get("title") or ""
    text = (page.get("text") or "").strip()
    if not text:
        return []
    header = f"Source URL: {url}"
    if title:
        header += f"\nTitle: {title}"
    return chunk_text(f"{header}\n\n{text}", max_length=1000, overlap=100)


def main() -> None:
    if not BUNDLE_PATH.exists():
        raise FileNotFoundError(f"Missing bundle: {BUNDLE_PATH}")

    payload = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    pages = payload.get("pages") or []

    removed_policy = delete_records_by_prefix(POLICY_PREFIX)
    removed_sitemap = delete_records_by_prefix("sitemap_")
    print(f"[fast-ingest] removed {removed_policy} policy_* and {removed_sitemap} sitemap_* KB record(s)")

    stored_chunks = 0
    stored_pages = 0
    for page in pages:
        chunks = _page_to_chunks(page)
        if not chunks:
            print(f"[fast-ingest] skip empty: {page.get('url')}")
            continue
        url = page["url"]
        for idx, chunk in enumerate(chunks):
            upsert_kb(get_embedding(chunk), chunk, _doc_id(url, idx), lable="general")
            stored_chunks += 1
        stored_pages += 1
        print(f"[fast-ingest] {len(chunks)} chunk(s) -> {url}")

    summary = {
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "bundle": str(BUNDLE_PATH),
        "stored_pages": stored_pages,
        "stored_chunks": stored_chunks,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
