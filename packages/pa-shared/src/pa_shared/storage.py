import asyncio
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from pa_shared.settings import get_settings


class StorageError(RuntimeError):
    pass


class MissingObject(StorageError):
    pass


@lru_cache
def s3_client(endpoint: str):
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=4,
            read_timeout=10,
            retries={"total_max_attempts": 2},
        ),
    )


async def object_operation(operation: str, key: str, **kwargs: Any) -> Any:
    settings = get_settings()
    client = s3_client(settings.s3_endpoint)
    try:
        return await asyncio.to_thread(
            getattr(client, operation), Bucket=settings.s3_bucket, Key=key, **kwargs
        )
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
            raise MissingObject("Object does not exist") from None
        raise StorageError("Object storage request failed") from None
    except BotoCoreError:
        raise StorageError("Object storage request failed") from None


async def get_object_bytes(key: str, *, max_bytes: int | None = None) -> bytes:
    body = (await object_operation("get_object", key))["Body"]

    def read() -> bytes:
        with body:
            if max_bytes is None:
                return body.read()
            data = body.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise StorageError("Object exceeds the allowed size")
            return data

    try:
        return await asyncio.to_thread(read)
    except BotoCoreError:
        raise StorageError("Object storage response failed") from None


def presign(operation: str, key: str, expires: int, **kwargs: Any) -> str:
    settings = get_settings()
    client = s3_client(settings.s3_public_endpoint or settings.s3_endpoint)
    return client.generate_presigned_url(
        operation,
        Params={"Bucket": settings.s3_bucket, "Key": key, **kwargs},
        ExpiresIn=expires,
    )
