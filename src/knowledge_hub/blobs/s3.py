"""S3-compatible blob store over boto3; every call runs in a worker thread."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_MISSING_CODES = {"NoSuchKey", "404", "NotFound"}


class BlobNotFound(KeyError):
    """The key does not exist in the store."""


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class S3BlobStore:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str) -> None:
        self.bucket = bucket
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
        )

    def _put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def _get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                raise BlobNotFound(key) from exc
            raise
        return response["Body"].read()

    def _delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._put, key, data, content_type)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get, key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)
