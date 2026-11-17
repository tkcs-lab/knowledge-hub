"""The s3 step: the bucket exists and takes a put, get and delete (plan 3.7 step 5)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from knowledge_hub.settings import Settings
from knowledge_hub.setup.steps import Change

_MISSING = {"404", "NoSuchBucket", "NotFound"}


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", "Unknown"))


class S3Step:
    name = "s3"

    def __init__(self, settings: Settings, *, create_bucket: bool = False, client: Any = None) -> None:
        if not settings.s3_bucket:
            raise ValueError("KH_S3_BUCKET is not set")
        self._bucket = settings.s3_bucket
        self._create = create_bucket
        if client is None:
            if not (settings.s3_endpoint and settings.s3_access_key_file and settings.s3_secret_key_file):
                raise ValueError(
                    "KH_S3_ENDPOINT, KH_S3_ACCESS_KEY_FILE and KH_S3_SECRET_KEY_FILE are required"
                )
            client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint,
                aws_access_key_id=settings.s3_access_key_file.read_text().strip(),
                aws_secret_access_key=settings.s3_secret_key_file.read_text().strip(),
                region_name=settings.s3_region,
                config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
            )
        self._client = client

    def _exists(self) -> Change | bool:
        """True when the bucket exists, False when it is missing, a ``!`` line on another error."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            if _code(exc) in _MISSING:
                return False
            return Change("!", f"bucket {self._bucket}: probe failed {_code(exc)}")
        return True

    def _probe(self) -> Change | None:
        key = f"kh-setup-probe/{uuid.uuid4()}"
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=b"kh setup probe")
            self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            return Change("!", f"bucket {self._bucket}: probe failed {_code(exc)}")
        return None

    def _plan(self) -> list[Change]:
        found = self._exists()
        if isinstance(found, Change):
            return [found]
        if not found:
            return [Change("+", f"bucket {self._bucket}")]
        line = self._probe()
        return [line] if line else []

    def _apply(self) -> list[Change]:
        found = self._exists()
        if isinstance(found, Change):
            return [found]
        if found:
            line = self._probe()
            return [line] if line else []
        if not self._create:
            return [Change("!", f"bucket {self._bucket}: does not exist; create it or pass --create-bucket")]
        try:
            self._client.create_bucket(Bucket=self._bucket)
        except ClientError as exc:
            return [Change("!", f"bucket {self._bucket}: create failed {_code(exc)}")]
        out = [Change("+", f"bucket {self._bucket}")]
        line = self._probe()
        return out + [line] if line else out

    async def plan(self) -> list[Change]:
        return await asyncio.to_thread(self._plan)

    async def apply(self, prune: bool) -> list[Change]:
        return await asyncio.to_thread(self._apply)
