"""Blob store test doubles."""

from __future__ import annotations

from botocore.stub import Stubber

from knowledge_hub.blobs.s3 import BlobNotFound, S3BlobStore

TEST_ENDPOINT = "http://blobs.test.example:9000"
TEST_BUCKET = "kh-test"


class MemoryBlobStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[key][0]
        except KeyError:
            raise BlobNotFound(key) from None

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def make_s3_store_with_stubber() -> tuple[S3BlobStore, Stubber]:
    """An `S3BlobStore` whose boto3 client is wrapped by an activated `Stubber`."""
    store = S3BlobStore(TEST_ENDPOINT, TEST_BUCKET, "test-access", "test-secret", "us-east-1")
    stubber = Stubber(store._client)
    stubber.activate()
    return store, stubber
