import os

import boto3
from dotenv import load_dotenv

load_dotenv()

R2_BUCKET = os.environ.get("R2_BUCKET", "jr-chatbot")
R2_ACCOUNT_ENDPOINT = os.environ.get(
    "R2_ACCOUNT_ENDPOINT",
    "https://b166538fe6434f1c027568fda8861597.r2.cloudflarestorage.com",
)
R2_PUBLIC_BASE = os.environ.get(
    "R2_PUBLIC_BASE",
    "https://pub-706af74a9f2443aa9e89918b8fd710b9.r2.dev/jr-chatbot",
)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ACCOUNT_ENDPOINT,
    aws_access_key_id=os.environ.get("R2_ACCESS_KEY"),
    aws_secret_access_key=os.environ.get("R2_SECRET_KEY"),
    region_name="auto",
)


def public_url_for_key(key: str) -> str:
    return f"{R2_PUBLIC_BASE.rstrip('/')}/{key.lstrip('/')}"


def upload_object_bytes(key: str, body: bytes, content_type: str) -> str:
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=body,
        ContentType=content_type,
    )
    return public_url_for_key(key)


def generate_presigned_put_url(key: str, content_type: str, expires_in: int = 60) -> str:
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
