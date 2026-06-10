#!/usr/bin/env python3
"""
Ingest scraped Jaipur Rugs sitemap pages into the Pinecone knowledge base.

Usage:
  python scripts/ingest_sitemap_to_kb.py --scrape   # scrape + ingest
  python scripts/ingest_sitemap_to_kb.py --input data/sitemap_scrape.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qlink_chatbot.database.pinecone_utils import (  # noqa: E402
    chunk_text,
    delete_records_by_prefix,
    get_embedding,
    upsert_kb,
)

SITEMAP_PREFIX = "sitemap_"
DEFAULT_SCRAPE_OUT = ROOT / "data" / "sitemap_scrape.json"
SCRAPE_SCRIPT = ROOT / "scripts" / "scrape_jaipurrugs_sitemap.mjs"
CF_MARKERS = (
    "performing security verification",
    "just a moment",
    "enable javascript and cookies",
    "waiting for www.jaipurrugs.com to respond",
)


def _url_doc_id(url: str, chunk_index: int) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"{SITEMAP_PREFIX}{digest}_{chunk_index}"


def _is_cloudflare_shell(page: dict) -> bool:
    blob = " ".join(
        str(page.get(key) or "")
        for key in ("title", "h1", "text")
    ).lower()
    return any(marker in blob for marker in CF_MARKERS)


def _page_to_chunks(page: dict) -> list[str]:
    url = page.get("url", "")
    title = page.get("title") or page.get("h1") or ""
    h1 = page.get("h1") or ""
    text = page.get("text") or ""
    if page.get("status") != "ok" or not text or _is_cloudflare_shell(page):
        return []

    header = f"Source URL: {url}"
    if title:
        header += f"\nTitle: {title}"
    if h1 and h1 != title:
        header += f"\nHeading: {h1}"

    full_text = f"{header}\n\n{text}"
    return chunk_text(full_text, max_length=1000, overlap=100)


def ingest_scrape_file(path: Path, *, replace_existing: bool = True) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    pages = payload.get("pages") or []

    if replace_existing:
        removed = delete_records_by_prefix(SITEMAP_PREFIX)
        print(f"[ingest] removed {removed} existing sitemap KB record(s)")

    stored_chunks = 0
    stored_pages = 0
    skipped_pages = 0

    for page in pages:
        chunks = _page_to_chunks(page)
        if not chunks:
            skipped_pages += 1
            continue

        url = page.get("url", "")
        for idx, chunk in enumerate(chunks):
            doc_id = _url_doc_id(url, idx)
            vector = get_embedding(chunk)
            upsert_kb(vector, chunk, doc_id, lable="general")
            stored_chunks += 1
        stored_pages += 1
        print(f"[ingest] stored {len(chunks)} chunk(s) for {url}")

    summary = {
        "scraped_at": payload.get("scraped_at"),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "input_file": str(path),
        "total_pages": len(pages),
        "stored_pages": stored_pages,
        "skipped_pages": skipped_pages,
        "stored_chunks": stored_chunks,
    }
    return summary


def run_scraper(out_path: Path) -> None:
    if not SCRAPE_SCRIPT.exists():
        raise FileNotFoundError(f"Scrape script not found: {SCRAPE_SCRIPT}")

    cmd = ["node", str(SCRAPE_SCRIPT), f"--out={out_path}"]
    print(f"[ingest] running scraper: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Jaipur Rugs sitemap and ingest into KB")
    parser.add_argument("--scrape", action="store_true", help="Run Playwright scraper before ingest")
    parser.add_argument("--input", type=Path, default=DEFAULT_SCRAPE_OUT, help="Scrape JSON input file")
    parser.add_argument("--keep-existing", action="store_true", help="Do not delete old sitemap_* KB records")
    args = parser.parse_args()

    input_path = args.input if args.input.is_absolute() else ROOT / args.input

    if args.scrape:
        run_scraper(input_path)

    if not input_path.exists():
        raise FileNotFoundError(
            f"Scrape file not found: {input_path}. Run with --scrape first."
        )

    summary = ingest_scrape_file(input_path, replace_existing=not args.keep_existing)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
