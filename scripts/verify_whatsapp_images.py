"""Phase 0 smoke: confirm Cloudinary wrap for a sample JR product image URL."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from qlink_chatbot.utils.whatsapp_images import get_whatsapp_safe_image_url

SAMPLE = (
    "https://images.jaipurrugs.com/prod-images/Headshot/Large/RCT_ADWL-13095-0002.jpg"
)


def main() -> int:
    cloud = (os.getenv("CLOUDINARY_CLOUD_NAME") or "").strip()
    safe = get_whatsapp_safe_image_url(SAMPLE)
    print(f"CLOUDINARY_CLOUD_NAME set: {bool(cloud)}")
    print(f"sample: {SAMPLE}")
    print(f"safe:   {safe}")
    if not cloud:
        print("WARN: set CLOUDINARY_CLOUD_NAME on Vultr for reliable WA image headers.")
        return 1
    if not safe or "res.cloudinary.com" not in safe:
        print("ERROR: expected Cloudinary fetch URL")
        return 2
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
