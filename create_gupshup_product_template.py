from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from set_gupshup_webhook import PARTNER_BASE_URL, ensure_ok, get_app_token


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

DEFAULT_TEMPLATE_NAME = "jaipur_rugs_product_cta"
DEFAULT_SEARCH_URL = "https://www.jaipurrugs.com/in/search"
DEFAULT_PRODUCT_URL = "https://www.jaipurrugs.com/in/rugs/pae-4250-desert-rose-desert-rose-rug"


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip()


def _require_first(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    raise SystemExit(
        "Missing required environment variable. Set one of: "
        + ", ".join(names)
    )


def _get_template_app_token(app_id: str) -> str:
    explicit_token = _env("QLINK_GUPSHUP_PARTNER_APP_TOKEN", "")
    if explicit_token:
        return explicit_token
    return get_app_token(app_id)


def _get_template_api_key() -> str:
    return _require_first("QLINK_GUPSHUP_API_KEY", "GUPSHUP_API_KEY")


def build_template_payload() -> dict[str, Any]:
    template_name = _env("GUPSHUP_PRODUCT_TEMPLATE_NAME", DEFAULT_TEMPLATE_NAME)
    product_url = _env("GUPSHUP_TEMPLATE_EXAMPLE_PRODUCT_URL", DEFAULT_PRODUCT_URL)
    search_url = _env("GUPSHUP_TEMPLATE_SEARCH_URL", DEFAULT_SEARCH_URL)
    content = "{{1}}\nTap below to view this rug on Jaipur Rugs."
    example = "Desert Rose Rug - Hand-knotted Wool, 6x9 ft, INR 45000.\nTap below to view this rug on Jaipur Rugs."
    buttons = [
        {
            "type": "URL",
            "text": "View Product",
            "url": "https://www.jaipurrugs.com/{{1}}",
            "buttonValue": product_url,
            "example": [product_url],
        },
    ]

    return {
        "elementName": template_name,
        "languageCode": _env("GUPSHUP_PRODUCT_TEMPLATE_LANGUAGE", "en"),
        "category": _env("GUPSHUP_PRODUCT_TEMPLATE_CATEGORY", "MARKETING"),
        "templateType": "TEXT",
        "vertical": _env("GUPSHUP_PRODUCT_TEMPLATE_VERTICAL", "Jaipur Rugs"),
        "content": content,
        "example": example,
        "enableSample": "true",
        "buttons": json.dumps(buttons),
    }


def submit_template(app_id: str, app_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{PARTNER_BASE_URL}/partner/app/{app_id}/templates"
    base_headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post(
        url,
        headers={**base_headers, "Authorization": app_token},
        data=payload,
        timeout=30,
    )
    if response.status_code in {401, 403}:
        response = requests.post(
            url,
            headers={**base_headers, "token": app_token},
            data=payload,
            timeout=30,
        )
    data = response.json() if response.content else {}
    message = str(data.get("message") or "")
    if response.status_code == 400 and "Template Not Supported" in message:
        api_key = _get_template_api_key()
        response = requests.post(
            f"https://api.gupshup.io/wa/app/{app_id}/template",
            headers={
                "apikey": api_key,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=payload,
            timeout=30,
        )
    return ensure_ok(response, "Submit product template")


def main() -> None:
    app_id = _require_first("QLINK_GUPSHUP_APP_ID", "GUPSHUP_APP_ID")
    app_token = _get_template_app_token(app_id)
    payload = build_template_payload()
    result = submit_template(app_id, app_token, payload)
    print(
        json.dumps(
            {
                "status": "submitted",
                "templateName": payload["elementName"],
                "message": result.get("message"),
                "template": result.get("template"),
                "raw": result,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
