import os

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

load_dotenv()

R2_BUCKET = (os.environ.get("R2_BUCKET") or "jr-chatbot").strip()
R2_ACCOUNT_ENDPOINT = (
    os.environ.get(
        "R2_ACCOUNT_ENDPOINT",
        "https://b166538fe6434f1c027568fda8861597.r2.cloudflarestorage.com",
    )
    or ""
).strip()
R2_PUBLIC_BASE = (
    os.environ.get(
        "R2_PUBLIC_BASE",
        "https://pub-706af74a9f2443aa9e89918b8fd710b9.r2.dev/jr-chatbot",
    )
    or ""
).strip()
R2_ACCESS_KEY = (os.environ.get("R2_ACCESS_KEY") or "").strip()
R2_SECRET_KEY = (os.environ.get("R2_SECRET_KEY") or "").strip()


class R2NotConfiguredError(RuntimeError):
    """Raised when R2 credentials or bucket settings are missing."""


def r2_is_configured() -> bool:
    return bool(R2_ACCESS_KEY and R2_SECRET_KEY and R2_ACCOUNT_ENDPOINT and R2_BUCKET)


def _require_r2_configured() -> None:
    if r2_is_configured():
        return
    missing = []
    if not R2_ACCESS_KEY:
        missing.append("R2_ACCESS_KEY")
    if not R2_SECRET_KEY:
        missing.append("R2_SECRET_KEY")
    if not R2_ACCOUNT_ENDPOINT:
        missing.append("R2_ACCOUNT_ENDPOINT")
    if not R2_BUCKET:
        missing.append("R2_BUCKET")
    raise R2NotConfiguredError(f"Missing R2 configuration: {', '.join(missing)}")


s3 = boto3.client(
    "s3",
    endpoint_url=R2_ACCOUNT_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY or None,
    aws_secret_access_key=R2_SECRET_KEY or None,
    region_name="auto",
    config=Config(signature_version="s3v4"),
)


def public_url_for_key(key: str) -> str:
    return f"{R2_PUBLIC_BASE.rstrip('/')}/{key.lstrip('/')}"


def upload_object_bytes(key: str, body: bytes, content_type: str) -> str:
    _require_r2_configured()
    try:
        s3.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
    except (ClientError, BotoCoreError) as err:
        raise RuntimeError(f"R2 put_object failed: {err}") from err
    return public_url_for_key(key)


def generate_presigned_put_url(key: str, content_type: str, expires_in: int = 60) -> str:
    _require_r2_configured()
    params = {
        "Bucket": R2_BUCKET,
        "Key": key,
        "ContentType": content_type,
    }
    return s3.generate_presigned_url(
        ClientMethod="put_object",
        Params=params,
        ExpiresIn=expires_in,
    )
