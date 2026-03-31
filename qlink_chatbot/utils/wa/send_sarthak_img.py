import httpx
import json
from qlink_chatbot.utils.logger_config import logger
import os

token = os.getenv("WHAPI_TOKEN")

def send_template_message(phone_number: str):
    """Sends a template message tos a phone number."""
    logger.info(
        "Sending template message to phone number with message",
        extra={"phone_number": phone_number},
    )
    destination = f"{phone_number}"

    url = (
        f"https://gate.whapi.cloud/messages/image"
    )

    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        "authorization": f"Bearer {token}"
    }

    data = json.dumps({
        "to": destination,
        "caption": "",
        "media": "https://ik.imagekit.io/0rf6agnve/jr_Sarthak.jpg"
    })

    try:
        response = httpx.post(url, headers=headers, data=data)
        logger.info(
            "Response",
            extra={
                "phone_number": phone_number,
                "response": response.json(),
            },
        )
    except Exception as e:
        logger.error(
            "Error in sending template message",
            extra={"phone_number": phone_number, "error": e},
        )
        raise e
