import json

import httpx
import pytest

from knowledge_hub.forgejo import ForgejoClient, ForgejoError, ForgejoRefused

TOKEN = "s3cret-token"


def make(handler):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = ForgejoClient("http://forgejo:3000/", TOKEN, "kh", transport=httpx.MockTransport(wrapped))
    return client, seen


def js(data, status=200):
    return httpx.Response(status, json=data)


async def test_compare_files():
    files = [
        {"filename": "a.md", "status": "modified"},
        {"filename": "b.md", "status": "renamed", "previous_filename": "c.md"},
    ]
    c, seen = make(lambda r: js({"files": files}))
    out = await c.compare_files("canonical-all", "main", "feat")
    assert out == files
    assert str(seen[0].url) == "http://forgejo:3000/api/v1/repos/kh/canonical-all/compare/main...feat"


async def test_head_sha_or_none():
    c, _ = make(lambda r: js({"commit": {"id": "abc"}}))
    assert await c.head_sha_or_none("r", "main") == "abc"
    c, _ = make(lambda r: js({}, 404))
    assert await c.head_sha_or_none("r", "main") is None
    c, _ = make(lambda r: js({}, 500))
    with pytest.raises(ForgejoError):
        await c.head_sha_or_none("r", "main")


async def test_team_id_second_page():
    def handler(r):
        if r.url.params["page"] == "1":
            return js([{"id": i, "name": f"t{i}"} for i in range(50)])
        return js([{"id": 99, "name": "owners"}])

    c, seen = make(handler)
    assert await c.team_id("owners") == 99
    assert len(seen) == 2
    assert seen[0].url.params["limit"] == "50"
    c, _ = make(lambda r: js([]))
    assert await c.team_id("owners") is None


@pytest.mark.parametrize("status,expected", [(204, True), (404, False)])
async def test_is_team_member(status, expected):
    c, seen = make(lambda r: httpx.Response(status))
    assert await c.is_team_member(7, "a b") is expected
    assert seen[0].url.raw_path.decode() == "/api/v1/teams/7/members/a%20b"


async def test_is_team_member_error():
    c, _ = make(lambda r: httpx.Response(500))
    with pytest.raises(ForgejoError):
        await c.is_team_member(7, "x")


async def test_post_status():
    c, seen = make(lambda r: js({}, 201))
    await c.post_status("canonical-all", "abc", "kh/validate", "success", "x" * 200)
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/repos/kh/canonical-all/statuses/abc"
    body = json.loads(req.content)
    assert body == {"context": "kh/validate", "state": "success", "description": "x" * 140}
    assert TOKEN not in req.content.decode()


@pytest.mark.parametrize("context,state", [("other", "success"), ("kh/validate", "bogus")])
async def test_post_status_refused(context, state):
    c, seen = make(lambda r: js({}, 201))
    with pytest.raises(ForgejoRefused):
        await c.post_status("r", "abc", context, state, "d")
    assert seen == []


async def test_post_status_error_has_no_token():
    c, _ = make(lambda r: js({"message": TOKEN}, 500))
    with pytest.raises(ForgejoError) as ei:
        await c.post_status("r", "abc", "kh/validate", "success", "d")
    assert TOKEN not in str(ei.value)


def _item(n):
    return {"id": n, "status": "success", "context": f"c{n}", "description": "d",
            "creator": {"login": "kh-ci", "id": 3}}


async def test_commit_statuses_shape():
    items = [_item(1), {"context": "x", "state": "failure", "description": "", "creator": None}]
    c, seen = make(lambda r: js(items))
    assert await c.commit_statuses("r", "abc") == [
        {"context": "c1", "state": "success", "description": "d", "creator": "kh-ci"},
        {"context": "x", "state": "failure", "description": "", "creator": None},
    ]
    assert seen[0].url.path == "/api/v1/repos/kh/r/commits/abc/statuses"


async def test_commit_statuses_pages_and_bound():
    def h(r):
        page = int(r.url.params["page"])
        return js([_item(page * 100 + i) for i in range(50)] if page < 3 else [_item(1)])

    c, seen = make(h)
    assert len(await c.commit_statuses("r", "abc")) == 101
    assert len(seen) == 3

    c, seen = make(lambda r: js([_item(i) for i in range(50)]))
    with pytest.raises(ForgejoError, match="too many statuses"):
        await c.commit_statuses("r", "abc")
    assert len(seen) == 20
