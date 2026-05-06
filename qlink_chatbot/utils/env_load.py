import os

from dotenv import load_dotenv

load_dotenv()

gupshup_api_key = os.getenv("GUPSHUP_API_KEY", "")
gupshup_app_name = os.getenv("GUPSHUP_APP_NAME", "")
qlink_app_name = os.getenv("QLINK_APP_NAME", gupshup_app_name)
gupshup_source = os.getenv("GUPSHUP_SOURCE", "")
default_country_code = os.getenv("DEFAULT_COUNTRY_CODE", "91")
