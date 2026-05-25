from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

PARTNER_BASE_URL = "https://partner.gupshup.io"
DEFAULT_MODES = "MESSAGE,SENT,DELIVERED,READ,DELETED,FAILED,OTHERS,ENQUEUED"


def require_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def parse_json_response(response: requests.Response) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError as exc:
        raise SystemExit(
            f"Unexpected non-JSON response from {response.request.method} {response.url}: "
            f"HTTP {response.status_code} {response.text[:500]}"
        ) from exc


def ensure_ok(response: requests.Response, context: str) -> dict[str, Any]:
    data = parse_json_response(response)
    if response.status_code >= 400:
        message = data.get("message") or data.get("status") or response.text[:500]
        raise SystemExit(f"{context} failed: HTTP {response.status_code} - {message}")
    return data


def get_partner_token() -> str:
    explicit_token = (os.environ.get("GUPSHUP_PARTNER_TOKEN") or "").strip()
    if explicit_token:
        return explicit_token

    email = (os.environ.get("GUPSHUP_PARTNER_EMAIL") or "").strip()
    password = (
        os.environ.get("GUPSHUP_PARTNER_CLIENT_SECRET")
        or os.environ.get("GUPSHUP_PARTNER_PASSWORD")
        or ""
    ).strip()
    if not email or not password:
        raise SystemExit(
            "Missing partner authentication. Set GUPSHUP_PARTNER_TOKEN, or both "
            "GUPSHUP_PARTNER_EMAIL and GUPSHUP_PARTNER_CLIENT_SECRET "
            "(or GUPSHUP_PARTNER_PASSWORD for older partner accounts)."
        )

    response = requests.post(
        f"{PARTNER_BASE_URL}/partner/account/login",
        data={"email": email, "password": password},
        timeout=30,
    )
    data = ensure_ok(response, "Partner login")
    token = (data.get("token") or "").strip()
    if not token:
        raise SystemExit("Partner login succeeded but no partner token was returned.")
    return token


def get_app_token(app_id: str) -> str:
    explicit_app_token = (os.environ.get("GUPSHUP_PARTNER_APP_TOKEN") or "").strip()
    if explicit_app_token:
        return explicit_app_token

    # Some projects already store the Gupshup app token in GUPSHUP_TOKEN.
    legacy_app_token = (os.environ.get("GUPSHUP_TOKEN") or "").strip()
    if legacy_app_token:
        return legacy_app_token

    partner_token = get_partner_token()
    response = requests.get(
        f"{PARTNER_BASE_URL}/partner/app/{app_id}/token",
        headers={"token": partner_token},
        timeout=30,
    )
    data = ensure_ok(response, "Fetch app token")
    token_data = data.get("token") or {}
    app_token = token_data.get("token") if isinstance(token_data, dict) else str(token_data)
    app_token = (app_token or "").strip()
    if not app_token:
        raise SystemExit("App token request succeeded but no PARTNER_APP_TOKEN was returned.")
    return app_token


def get_existing_subscriptions(app_id: str, app_token: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{PARTNER_BASE_URL}/partner/app/{app_id}/subscription",
        headers={"Authorization": app_token},
        timeout=30,
    )
    data = ensure_ok(response, "Fetch subscriptions")
    subscriptions = data.get("subscriptions") or []
    if not isinstance(subscriptions, list):
        raise SystemExit("Unexpected subscriptions payload returned by Gupshup.")
    return subscriptions


def upsert_subscription(app_id: str, app_token: str, webhook_url: str) -> dict[str, Any]:
    tag = (os.environ.get("GUPSHUP_WEBHOOK_TAG") or "jaipur-rugs-backend").strip()
    version = (os.environ.get("GUPSHUP_WEBHOOK_VERSION") or "3").strip()
    modes = (os.environ.get("GUPSHUP_WEBHOOK_MODES") or DEFAULT_MODES).strip()
    show_on_ui = (os.environ.get("GUPSHUP_WEBHOOK_SHOW_ON_UI") or "true").strip().lower()

    payload = {
        "url": webhook_url,
        "tag": tag,
        "version": version,
        "modes": modes,
        "showOnUI": "true" if show_on_ui in {"1", "true", "yes"} else "false",
        "active": "true",
        "doCheck": "false",
    }

    subscriptions = get_existing_subscriptions(app_id, app_token)
    existing = next(
        (
            subscription
            for subscription in subscriptions
            if subscription.get("tag") == tag or subscription.get("url") == webhook_url
        ),
        None,
    )

    if existing:
        subscription_id = str(existing.get("id") or "").strip()
        if not subscription_id:
            raise SystemExit("Found matching subscription but it has no subscription id.")
        response = requests.put(
            f"{PARTNER_BASE_URL}/partner/app/{app_id}/subscription/{subscription_id}",
            headers={
                "Authorization": app_token,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=payload,
            timeout=30,
        )
        data = ensure_ok(response, "Update subscription")
        return {"action": "updated", "subscription": data.get("subscription") or {}}

    response = requests.post(
        f"{PARTNER_BASE_URL}/partner/app/{app_id}/subscription",
        headers={
            "Authorization": app_token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=payload,
        timeout=30,
    )
    data = ensure_ok(response, "Create subscription")
    return {"action": "created", "subscription": data.get("subscription") or {}}


def main() -> None:
    app_id = require_env("GUPSHUP_APP_ID")
    webhook_url = require_env("WEBHOOK_URL")
    app_token = get_app_token(app_id)
    result = upsert_subscription(app_id, app_token, webhook_url)
    subscription = result["subscription"]
    print(
        json.dumps(
            {
                "status": "success",
                "action": result["action"],
                "appId": app_id,
                "subscriptionId": subscription.get("id"),
                "url": subscription.get("url"),
                "tag": subscription.get("tag"),
                "version": subscription.get("version"),
                "modes": subscription.get("modes"),
                "active": subscription.get("active"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
