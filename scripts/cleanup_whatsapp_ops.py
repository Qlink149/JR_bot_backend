"""Delete old WhatsApp ops collections (dedupe / outbound / status).

Usage:
  python scripts/cleanup_whatsapp_ops.py
  python scripts/cleanup_whatsapp_ops.py --days 7
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qlink_chatbot.database.mongo_utils import cleanup_whatsapp_ops_collections


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()
    deleted = cleanup_whatsapp_ops_collections(older_than_days=args.days)
    print(deleted)


if __name__ == "__main__":
    main()
