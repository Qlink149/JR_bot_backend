from __future__ import annotations

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from set_gupshup_webhook import PARTNER_BASE_URL, ensure_ok, get_app_token

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")


def _require_first(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    raise SystemExit(
        "Missing required environment variable. Set one of: " + ", ".join(names)
    )


def _get_template_app_token(app_id: str) -> str:
    explicit_token = (os.environ.get("QLINK_GUPSHUP_PARTNER_APP_TOKEN") or "").strip()
    if explicit_token:
        return explicit_token
    return get_app_token(app_id)


def list_templates(app_id: str, app_token: str) -> list[dict]:
    response = requests.get(
        f"{PARTNER_BASE_URL}/partner/app/{app_id}/templates",
        headers={"Authorization": app_token},
        timeout=30,
    )
    data = ensure_ok(response, "List templates")
    templates = data.get("templates") or []
    if not isinstance(templates, list):
        raise SystemExit(f"Unexpected templates payload: {data}")
    return templates


def main() -> None:
    app_id = _require_first("QLINK_GUPSHUP_APP_ID", "GUPSHUP_APP_ID")
    app_token = _get_template_app_token(app_id)
    templates = list_templates(app_id, app_token)

    filter_name = (
        os.environ.get("GUPSHUP_PRODUCT_TEMPLATE_NAME") or "jaipur_rugs_product_cta"
    ).strip()

    matched = [t for t in templates if t.get("elementName") == filter_name]
    others = [t for t in templates if t.get("elementName") != filter_name]

    print(f"\n=== Template: {filter_name} ===")
    if matched:
        for t in matched:
            print(
                json.dumps(
                    {
                        "elementName": t.get("elementName"),
                        "status": t.get("status"),
                        "category": t.get("category"),
                        "languageCode": t.get("languageCode"),
                        "templateType": t.get("templateType"),
                        "buttons": t.get("buttons"),
                        "rejectionReason": t.get("rejectionReason"),
                    },
                    indent=2,
                )
            )
    else:
        print(f'  [NOT FOUND] No template named "{filter_name}" on this app.')

    jr_related = [
        t for t in others
        if "jaipur" in (t.get("elementName") or "").lower()
        or (t.get("elementName") or "").startswith("jr_")
    ]
    if jr_related:
        print(f"\n=== Jaipur Rugs related templates ({len(jr_related)}) ===")
        for t in jr_related:
            print(
                json.dumps(
                    {
                        "elementName": t.get("elementName"),
                        "status": t.get("status"),
                        "category": t.get("category"),
                        "languageCode": t.get("languageCode"),
                        "templateType": t.get("templateType"),
                        "content": t.get("content"),
                        "buttons": t.get("buttons"),
                        "rejectionReason": t.get("rejectionReason"),
                    },
                    indent=2,
                )
            )

    remaining = [t for t in others if t not in jr_related]
    if remaining:
        print(f"\n=== Other templates ({len(remaining)}) ===")
        for t in remaining:
            print(
                f"  {t.get('elementName')!r:45s}  status={t.get('status')}"
            )


if __name__ == "__main__":
    main()
