"""One-time / ops: rebuild search_tokens from product.raw using current tokenizer.

Usage (from JR_bot_backend with .env loaded):
  python scripts/backfill_search_tokens.py
  python scripts/backfill_search_tokens.py --rebuild-all --batch-size 2000
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from bson import ObjectId

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)

from qlink_chatbot.utils.jr_search_index import backfill_search_tokens  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument(
        "--rebuild-all",
        action="store_true",
        help="Rebuild tokens for every product (use after tokenizer changes)",
    )
    args = parser.parse_args()

    batch = 0
    total_updated = 0
    after_id = None
    while True:
        batch += 1
        t0 = time.time()
        result = backfill_search_tokens(
            batch_size=args.batch_size,
            rebuild_all=args.rebuild_all,
            after_id=after_id,
        )
        total_updated += int(result.get("updated") or 0)
        remaining = int(result.get("remaining_without_tokens") or 0)
        last_id = result.get("last_id")
        print(
            f"batch={batch} updated={result.get('updated')} "
            f"remaining={remaining} last_id={last_id} "
            f"elapsed={time.time() - t0:.1f}s total_updated={total_updated}"
        )
        if int(result.get("updated") or 0) == 0:
            break
        if args.rebuild_all:
            if not last_id:
                break
            after_id = ObjectId(last_id)
        elif remaining == 0:
            break

    print("done", {"total_updated": total_updated, "remaining": remaining})


if __name__ == "__main__":
    main()
