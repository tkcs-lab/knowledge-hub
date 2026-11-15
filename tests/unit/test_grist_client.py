"""GristClient against an ``httpx.MockTransport`` (plan 3.12)."""

import httpx
import pytest

from knowledge_hub.grist.client import GristClient, GristError


def client(handler) -> GristClient:
    return GristClient("https://grist.test/", "doc1", "dummy-key", transport=httpx.MockTransport(handler))


async def test_fetch_table_returns_fields_with_bearer_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json={"records": [{"id": 1, "fields": {"key": "prd:gateway", "name": "Gateway"}},
                              {"id": 2, "fields": {"key": "prd:api"}}]},
        )

    rows = await client(handler).fetch_table("Products")
    assert rows == [{"key": "prd:gateway", "name": "Gateway"}, {"key": "prd:api"}]
    assert seen == {"url": "https://grist.test/api/docs/doc1/tables/Products/records",
                    "auth": "Bearer dummy-key"}


async def test_empty_table():
    rows = await client(lambda r: httpx.Response(200, json={"records": []})).fetch_table("Versions")
    assert rows == []


@pytest.mark.parametrize("response", [httpx.Response(403), httpx.Response(500, text="boom")])
async def test_http_error_raises_grist_error(response):
    with pytest.raises(GristError, match="Customers: HTTP"):
        await client(lambda r: response).fetch_table("Customers")


async def test_bad_body_raises_grist_error():
    with pytest.raises(GristError, match="unexpected"):
        await client(lambda r: httpx.Response(200, json={"nope": 1})).fetch_table("Products")


async def test_transport_error_raises_grist_error():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(GristError, match="ConnectError"):
        await client(handler).fetch_table("Products")
