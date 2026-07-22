"""S3-compatible object storage per API_SPEC.md §8. R2 in production, MinIO
locally — endpoint_url is the only difference, so this module works unchanged
against either. Presigning is pure computation (no network call); HEAD and
DELETE are real I/O and run off the event loop via asyncio.to_thread.
"""

import asyncio
import contextlib
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import get_settings

PUT_TTL = 600
GET_TTL = 300


@lru_cache
def get_s3_client() -> Any:
    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint_url,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=BotoConfig(signature_version="s3v4"),
    )
    try:
        client.create_bucket(Bucket=settings.r2_bucket)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
    return client


def presign_put(key: str, content_type: str, size_bytes: int) -> tuple[str, dict[str, str]]:
    client = get_s3_client()
    url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": get_settings().r2_bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": size_bytes,
        },
        ExpiresIn=PUT_TTL,
    )
    return url, {"Content-Type": content_type, "Content-Length": str(size_bytes)}


def presign_get(key: str) -> str:
    client = get_s3_client()
    result: str = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": get_settings().r2_bucket, "Key": key},
        ExpiresIn=GET_TTL,
    )
    return result


def _head(key: str) -> tuple[bool, int | None]:
    client = get_s3_client()
    try:
        resp = client.head_object(Bucket=get_settings().r2_bucket, Key=key)
        return True, resp["ContentLength"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return False, None
        raise


async def head_object(key: str) -> tuple[bool, int | None]:
    return await asyncio.to_thread(_head, key)


def _delete(key: str) -> None:
    client = get_s3_client()
    with contextlib.suppress(ClientError):
        client.delete_object(Bucket=get_settings().r2_bucket, Key=key)


async def delete_object(key: str) -> None:
    """Best-effort — orphan sweep shouldn't fail the tick over a missing object."""
    await asyncio.to_thread(_delete, key)
