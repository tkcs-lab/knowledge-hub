import json

import httpx
import pytest

from knowledge_hub.forgejo import ForgejoClient, ForgejoError, ForgejoRefused
from knowledge_hub.forgejo.client import _guard_ref
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake

TOKEN = "s3cret-token"
BAD_REFS = [
    "main", "master", "bot/promote/", "bot/promote/../main", "bot/other/x", "feature/x", "",
    "refs/heads/main",
]


def make(handler):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = ForgejoClient("http://forgejo:3000/", TOKEN, "kh", transport=httpx.MockTransport(wrapped))
    return client, seen


def ok(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and request.url.path.endswith("/labels"):
        return httpx.Response(200, json=[{"id": 7, "name": "promotion"}, {"id": 8, "name": "domain/x"}])
    if request.method == "GET":
        return httpx.Response(404, json={})
    return httpx.Response(201, json={"number": 3})


def test_guard_accepts_bot_ref_and_refuses_others():
    assert _guard_ref("bot/promote/x") == "bot/promote/x"
    for ref in BAD_REFS:
        with pytest.raises(ForgejoRefused):
            _guard_ref(ref)


@pytest.mark.parametrize("ref", BAD_REFS)
async def test_every_write_refuses_bad_ref_without_sending(ref):
    c, seen = make(ok)
    with pytest.raises(ForgejoRefused):
        await c.create_branch("r", ref, "abc")
    with pytest.raises(ForgejoRefused):
        await c.put_file("r", ref, "product/a.md", "x", "m")
    with pytest.raises(ForgejoRefused):
        await c.open_pull("r", ref, "main", "t", "b", [])
    assert seen == []


async def test_open_pull_refuses_base_other_than_main():
    c, seen = make(ok)
    for base in ("develop", "bot/promote/x", ""):
        with pytest.raises(ForgejoRefused):
            await c.open_pull("r", "bot/promote/x", base, "t", "b", [])
    assert seen == []


async def test_write_requests_are_shaped():
    c, seen = make(ok)
    await c.create_branch("r", "bot/promote/x", "abc")
    await c.put_file("r", "bot/promote/x", "product/a b.md", "héllo", "msg")
    pull = await c.open_pull("r", "bot/promote/x", "main", "T", "B", ["promotion", "domain/x"])
    assert pull == {"number": 3}
    br = seen[0]
    assert (br.method, br.url.path) == ("POST", "/api/v1/repos/kh/r/branches")
    assert json.loads(br.content) == {"new_branch_name": "bot/promote/x", "old_ref_name": "abc"}
    put = [r for r in seen if r.method == "POST" and "contents" in r.url.path][0]
    assert put.url.raw_path.split(b"?")[0] == b"/api/v1/repos/kh/r/contents/product/a%20b.md"
    assert json.loads(put.content)["branch"] == "bot/promote/x"
    last = seen[-1]
    assert (last.method, last.url.path) == ("POST", "/api/v1/repos/kh/r/pulls")
    assert json.loads(last.content) == {
        "head": "bot/promote/x", "base": "main", "title": "T", "body": "B", "labels": [7, 8]
    }


async def test_put_file_updates_existing_with_sha():
    def h(r):
        if r.method == "GET":
            return httpx.Response(200, json={"sha": "blob1"})
        return httpx.Response(200, json={})

    c, seen = make(h)
    await c.put_file("r", "bot/promote/x", "a.md", "x", "m")
    assert seen[-1].method == "PUT"
    assert json.loads(seen[-1].content)["sha"] == "blob1"


async def test_path_exists_on_main():
    def h(r):
        assert r.url.params["ref"] == "main"
        return httpx.Response(200 if r.url.path.endswith("/product/a.md") else 404, json={})

    c, seen = make(h)
    assert await c.path_exists("r", "product/a.md", "main") is True
    assert await c.path_exists("r", "product/b.md", "main") is False
    assert all(r.method == "GET" for r in seen)


async def test_errors_carry_no_token():
    c, _ = make(lambda r: httpx.Response(500, text=f"boom {TOKEN}"))
    for call in (
        c.create_branch("r", "bot/promote/x", "abc"),
        c.path_exists("r", "a.md", "main"),
    ):
        with pytest.raises(ForgejoError) as ei:
            await call
        assert TOKEN not in str(ei.value)
        assert "HTTP 500" in str(ei.value)

    def boom(r):
        raise httpx.ConnectError(f"cannot reach {TOKEN}")

    c, _ = make(boom)
    with pytest.raises(ForgejoError) as ei:
        await c.open_pull("r", "bot/promote/x", "main", "t", "b", [])
    assert TOKEN not in str(ei.value) and TOKEN not in repr(ei.value)


async def test_open_pull_missing_label_is_an_error():
    c, _ = make(ok)
    with pytest.raises(ForgejoError, match="label not found"):
        await c.open_pull("r", "bot/promote/x", "main", "t", "b", ["nope"])


async def test_fake_records_written_refs():
    fake = ForgejoFake({"r": FakeRepo([FakeCommit("c1", files={"product/a.md": "A"})])})
    await fake.create_branch("r", "bot/promote/x", "c1")
    await fake.put_file("r", "bot/promote/x", "product/b.md", "B", "m")
    pull = await fake.open_pull("r", "bot/promote/x", "main", "t", "b", ["promotion"])
    assert pull["number"] == 1
    assert fake.written_refs == ["bot/promote/x"] * 3
    assert "main" not in fake.written_refs
    assert await fake.path_exists("r", "product/a.md", "main")
    assert not await fake.path_exists("r", "product/b.md", "main")
    assert await fake.path_exists("r", "product/b.md", "bot/promote/x")
