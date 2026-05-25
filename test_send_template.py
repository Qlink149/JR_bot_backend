"""Quick test: send the approved jaipur_view_product template to a given number."""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

API_KEY = os.environ.get("QLINK_GUPSHUP_API_KEY", "").strip()
SOURCE   = os.environ.get("QLINK_GUPSHUP_SOURCE", "").strip()
APP_NAME = os.environ.get("QLINK_GUPSHUP_APP_NAME", "").strip()
COUNTRY  = os.environ.get("DEFAULT_COUNTRY_CODE", "91").strip()

TEMPLATE_NAME = os.environ.get("GUPSHUP_PRODUCT_TEMPLATE_NAME", "jaipur_view_product").strip()
DESTINATION   = "9306704311"

# Sample product data to preview how the template looks with real content
SAMPLE_CAPTION     = "Desert Rose Rug – Hand-knotted, 6x9 ft, Wool. Perfect for your living room."
SAMPLE_PRODUCT_URL = "https://www.jaipurrugs.com/in/rugs/pae-4250-desert-rose-desert-rose-rug"
JAIPURRUGS_BASE    = "https://www.jaipurrugs.com/"


def normalize(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"{COUNTRY}{digits}" if len(digits) == 10 else digits


def url_suffix(full_url: str) -> str:
    return full_url[len(JAIPURRUGS_BASE):] if full_url.startswith(JAIPURRUGS_BASE) else full_url


def main() -> None:
    if not API_KEY or not SOURCE:
        raise SystemExit("Missing QLINK_GUPSHUP_API_KEY or QLINK_GUPSHUP_SOURCE in .env")

    dest = normalize(DESTINATION)
    url  = "https://api.gupshup.io/wa/api/v1/template/msg"

    template_payload = {
        "id": TEMPLATE_NAME,
        "params": [SAMPLE_CAPTION],
        "buttons": [{"type": "url", "parameter": url_suffix(SAMPLE_PRODUCT_URL)}],
    }

    data = {
        "channel":     "whatsapp",
        "source":      SOURCE,
        "destination": dest,
        "template":    json.dumps(template_payload),
        "src.name":    APP_NAME,
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded", "apikey": API_KEY}

    print(f"Sending template '{TEMPLATE_NAME}' to {dest} ...")
    resp = requests.post(url, headers=headers, data=data, timeout=30)
    print(f"Status: {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2))
    except Exception:
        print(resp.text)


if __name__ == "__main__":
    main()
