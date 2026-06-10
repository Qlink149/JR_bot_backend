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
# r2.dev public URLs are bucket-scoped — object keys must NOT repeat the bucket name.
R2_PUBLIC_BASE = (
    os.environ.get(
        "R2_PUBLIC_BASE",
        "https://pub-706af74a9f2443aa9e89918b8fd710b9.r2.dev",
    )
    or ""
).strip()


class R2NotConfiguredError(RuntimeError):
    """Raised when R2 credentials or bucket settings are missing."""


def _r2_credentials() -> tuple[str, str]:
    access_key = (os.environ.get("R2_ACCESS_KEY") or "").strip()
    secret_key = (os.environ.get("R2_SECRET_KEY") or "").strip()
    return access_key, secret_key


def r2_status() -> dict[str, bool]:
    access_key, secret_key = _r2_credentials()
    return {
        "configured": bool(access_key and secret_key and R2_ACCOUNT_ENDPOINT and R2_BUCKET),
        "access_key_set": bool(access_key),
        "secret_key_set": bool(secret_key),
        "bucket_set": bool(R2_BUCKET),
        "endpoint_set": bool(R2_ACCOUNT_ENDPOINT),
    }


def r2_is_configured() -> bool:
    return r2_status()["configured"]


def _require_r2_configured() -> None:
    access_key, secret_key = _r2_credentials()
    if r2_is_configured():
        return
    missing = []
    if not access_key:
        missing.append("R2_ACCESS_KEY")
    if not secret_key:
        missing.append("R2_SECRET_KEY")
    if not R2_ACCOUNT_ENDPOINT:
        missing.append("R2_ACCOUNT_ENDPOINT")
    if not R2_BUCKET:
        missing.append("R2_BUCKET")
    raise R2NotConfiguredError(f"Missing R2 configuration: {', '.join(missing)}")


def _s3_client():
    access_key, secret_key = _r2_credentials()
    return boto3.client(
        "s3",
        endpoint_url=R2_ACCOUNT_ENDPOINT,
        aws_access_key_id=access_key or None,
        aws_secret_access_key=secret_key or None,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def public_url_for_key(key: str) -> str:
    base = R2_PUBLIC_BASE.rstrip("/")
    # Guard against misconfigured env that duplicates the bucket segment in the path.
    if R2_BUCKET and base.endswith(f"/{R2_BUCKET}"):
        base = base[: -len(f"/{R2_BUCKET}")]
    return normalize_public_image_url(f"{base}/{key.lstrip('/')}")


def normalize_public_image_url(url: str) -> str:
    """Fix r2.dev URLs that incorrectly include the bucket name in the path."""
    if not url or not R2_BUCKET:
        return url
    marker = f".r2.dev/{R2_BUCKET}/"
    if marker in url:
        return url.replace(marker, ".r2.dev/", 1)
    return url


def upload_object_bytes(key: str, body: bytes, content_type: str) -> str:
    _require_r2_configured()
    try:
        _s3_client().put_object(
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
    return _s3_client().generate_presigned_url(
        ClientMethod="put_object",
        Params=params,
        ExpiresIn=expires_in,
    )
