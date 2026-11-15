from __future__ import annotations

import io

import pytest
from botocore.response import StreamingBody

from knowledge_hub.blobs import BlobNotFound
from tests.support.blobs import TEST_BUCKET, TEST_ENDPOINT, MemoryBlobStore, make_s3_store_with_stubber


async def test_memory_round_trip_and_delete() -> None:
    store = MemoryBlobStore()
    await store.put("a/b", b"hello", "text/plain")
    assert await store.get("a/b") == b"hello"
    await store.delete("a/b")
    with pytest.raises(BlobNotFound):
        await store.get("a/b")
    await store.delete("a/b")  # deleting a missing key is not an error


async def test_memory_not_found() -> None:
    with pytest.raises(BlobNotFound):
        await MemoryBlobStore().get("nope")


def test_s3_client_uses_path_style_and_endpoint() -> None:
    store, _ = make_s3_store_with_stubber()
    assert store._client.meta.config.s3["addressing_style"] == "path"
    assert store._client.meta.config.signature_version == "s3v4"
    assert store._client.meta.endpoint_url == TEST_ENDPOINT


async def test_s3_put_builds_request() -> None:
    store, stubber = make_s3_store_with_stubber()
    stubber.add_response(
        "put_object",
        {},
        {"Bucket": TEST_BUCKET, "Key": "raw/1", "Body": b"data", "ContentType": "text/markdown"},
    )
    await store.put("raw/1", b"data", "text/markdown")
    stubber.assert_no_pending_responses()


async def test_s3_get_returns_bytes() -> None:
    store, stubber = make_s3_store_with_stubber()
    body = StreamingBody(io.BytesIO(b"payload"), len(b"payload"))
    stubber.add_response("get_object", {"Body": body}, {"Bucket": TEST_BUCKET, "Key": "raw/1"})
    assert await store.get("raw/1") == b"payload"
    stubber.assert_no_pending_responses()


async def test_s3_delete_builds_request() -> None:
    store, stubber = make_s3_store_with_stubber()
    stubber.add_response("delete_object", {}, {"Bucket": TEST_BUCKET, "Key": "raw/1"})
    await store.delete("raw/1")
    stubber.assert_no_pending_responses()


async def test_s3_missing_key_maps_to_blob_not_found() -> None:
    store, stubber = make_s3_store_with_stubber()
    stubber.add_client_error(
        "get_object",
        service_error_code="NoSuchKey",
        http_status_code=404,
        expected_params={"Bucket": TEST_BUCKET, "Key": "x"},
    )
    with pytest.raises(BlobNotFound):
        await store.get("x")


async def test_s3_other_errors_propagate() -> None:
    from botocore.exceptions import ClientError

    store, stubber = make_s3_store_with_stubber()
    stubber.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
    with pytest.raises(ClientError):
        await store.get("x")
