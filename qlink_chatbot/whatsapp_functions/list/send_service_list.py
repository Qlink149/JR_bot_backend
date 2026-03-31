import json

import httpx

from qlink_chatbot.constants import GUPSHUP_SOURCE, QLINK_SOURCE
from qlink_chatbot.models.enums import ListIds
from qlink_chatbot.utils.env_load import (
    gupshup_api_key,
    gupshup_app_name,
    qlink_app_name,
)
from qlink_chatbot.utils.logger_config import logger


def send_service_list(phone_number):
    """Send a list message to a phone number."""
    url = "https://api.gupshup.io/wa/api/v1/msg"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "apikey": gupshup_api_key,
    }

    messages = (
        "👋 Welcome to Nilkamal Sleep!\n\n"
        "We're here to help you sleep better 😴\n\n"
        "Please choose an option to continue:\n"
        "1️⃣ Sales – Find the perfect mattress for you  \n"
        "2️⃣ Support – Help with an existing order\n\n"
        "Reply with 1 or 2 to get started."
    )

    message_json = json.dumps(
        {
            "type": "list",
            "title": "",
            "body": messages,
            "footer": "Managed by Nilkamal Sleep.",
            "msgid": f"{ListIds.SERVICE_LIST_ID.value}",
            "globalButtons": [{"type": "text", "title": "Options"}],
            "items": [
                {
                    "title": "Options",
                    "subtitle": "Choose a service",
                    "options": [
                        {
                            "type": "text",
                            "title": "Sales",
                            "description": "Find the perfect mattress for you",
                            "postbackText": "sales",
                        },
                        {
                            "type": "text",
                            "title": "Support",
                            "description": "Help with an existing order",
                            "postbackText": "support",
                        },
                    ],
                }
            ],
        }
    )

    data = {
        "source": GUPSHUP_SOURCE,
        "destination": f"{phone_number}",
        "src.name": gupshup_app_name,
        "message": message_json,
    }

    try:
        response = httpx.post(url, headers=headers, data=data)
        logger.info(
            "Response from Gupshup API for sending service list",
            extra={"response": response.json()},
        )
    except Exception as e:
        logger.error("Error in sending list", extra={"error": e})


def send_support_list(phone_number):
    """Send a support list message to a phone number."""
    url = "https://api.gupshup.io/wa/api/v1/msg"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "apikey": gupshup_api_key,
    }

    messages = (
        "We're here to help you 😊\n\n"
        "Please tell us what you need assistance with:\n"
        "1️⃣ Track your order  \n"
        "2️⃣ Raise a complaint  \n\n"
        "Reply with 1 or 2 to continue."
    )

    message_json = json.dumps(
        {
            "type": "list",
            "title": "",
            "body": messages,
            "footer": "Managed by Nilkamal Sleep.",
            "msgid": f"{ListIds.SUPPORT_LIST_ID.value}",
            "globalButtons": [{"type": "text", "title": "Options"}],
            "items": [
                {
                    "title": "Support Options",
                    "subtitle": "Choose an option",
                    "options": [
                        {
                            "type": "text",
                            "title": "Track Order",
                            "description": "Track your order status",
                            "postbackText": "track_order",
                        },
                        {
                            "type": "text",
                            "title": "Raise Complaint",
                            "description": "Submit a complaint",
                            "postbackText": "raise_complaint",
                        },
                    ],
                }
            ],
        }
    )

    data = {
        "source": GUPSHUP_SOURCE,
        "destination": f"{phone_number}",
        "src.name": gupshup_app_name,
        "message": message_json,
    }

    try:
        response = httpx.post(url, headers=headers, data=data)
        logger.info(
            "Response from Gupshup API for sending support list",
            extra={"response": response.json()},
        )
    except Exception as e:
        logger.error("Error in sending support list", extra={"error": e})