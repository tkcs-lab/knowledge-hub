import httpx
import pytest

from knowledge_hub.forgejo import ForgejoClient, ForgejoError, ForgejoRefused
from knowledge_hub.forgejo.client import _guard_ref

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


async def test_head_sha():
    c, seen = make(lambda r: js({"name": "main", "commit": {"id": "abc"}}))
    assert await c.head_sha("canonical-all", "main") == "abc"
    assert str(seen[0].url) == "http://forgejo:3000/api/v1/repos/kh/canonical-all/branches/main"
    assert seen[0].headers["authorization"] == f"token {TOKEN}"


async def test_list_tree_pages():
    def h(r):
        assert r.url.path == "/api/v1/repos/kh/r/git/trees/s1"
        assert r.url.params["recursive"] == "true"
        return js({"tree": [{"path": "a.md", "type": "blob", "sha": "x"}], "total_count": 1})

    c, _ = make(h)
    assert await c.list_tree("r", "s1") == [{"path": "a.md", "type": "blob", "sha": "x"}]


async def test_get_raw():
    def h(r):
        assert r.url.path == "/api/v1/repos/kh/r/raw/product/a b.md"
        assert r.url.params["ref"] == "s1"
        return httpx.Response(200, content="héllo".encode())

    c, _ = make(h)
    assert await c.get_raw("r", "product/a b.md", "s1") == "héllo"


async def test_commits_first_parent_follows_parents_zero_and_stops_at_since():
    commits = [
        {"sha": "c4", "parents": [{"sha": "c3"}, {"sha": "side"}]},
        {"sha": "side", "parents": [{"sha": "c1"}]},
        {"sha": "c3", "parents": [{"sha": "c2"}]},
        {"sha": "c2", "parents": [{"sha": "c1"}]},
        {"sha": "c1", "parents": []},
    ]

    def h(r):
        if r.url.path.endswith("/branches/main"):
            return js({"commit": {"id": "c4"}})
        assert r.url.path == "/api/v1/repos/kh/r/commits"
        assert r.url.params["sha"] == "c4"
        return js(commits)

    c, _ = make(h)
    assert [x["sha"] for x in await c.commits_first_parent("r", "main", None)] == ["c4", "c3", "c2", "c1"]
    assert [x["sha"] for x in await c.commits_first_parent("r", "main", "c2")] == ["c4", "c3"]


async def test_pull_for_commit_and_missing():
    def h(r):
        if "/commits/good/pull" in r.url.path:
            return js({"number": 7, "merge_commit_sha": "good"})
        return httpx.Response(404, json={"message": "nope"})

    c, seen = make(h)
    assert (await c.pull_for_commit("r", "good"))["number"] == 7
    assert str(seen[0].url) == "http://forgejo:3000/api/v1/repos/kh/r/commits/good/pull"
    assert await c.pull_for_commit("r", "bad") is None


async def test_pull_and_reviews():
    def h(r):
        if r.url.path.endswith("/pulls/7"):
            return js({"number": 7, "head": {"sha": "h"}})
        assert r.url.path == "/api/v1/repos/kh/r/pulls/7/reviews"
        return js([{"state": "APPROVED", "official": True, "commit_id": "h"}])

    c, _ = make(h)
    assert (await c.pull("r", 7))["head"]["sha"] == "h"
    assert (await c.pull_reviews("r", 7))[0]["state"] == "APPROVED"


async def test_changed_files():
    def h(r):
        assert r.url.raw_path.split(b"?")[0] == b"/api/v1/repos/kh/r/compare/a...b"
        return js({"files": [{"filename": "x.md"}, {"filename": "y/z.md"}]})

    c, _ = make(h)
    assert await c.changed_files("r", "a", "b") == ["x.md", "y/z.md"]


async def test_non_200_raises_without_token():
    c, _ = make(lambda r: httpx.Response(500, text=f"boom {TOKEN}"))
    with pytest.raises(ForgejoError) as ei:
        await c.pull("r", 1)
    assert TOKEN not in str(ei.value)
    assert "500" in str(ei.value)


async def test_transport_error_has_no_token():
    def h(r):
        raise httpx.ConnectError(f"cannot connect with {TOKEN}")

    c, _ = make(h)
    with pytest.raises(ForgejoError) as ei:
        await c.head_sha("r", "main")
    assert TOKEN not in str(ei.value)


@pytest.mark.parametrize(
    "ref", ["main", "refs/heads/main", "feature/x", "", "bot/promote/", "bot/promote/../x"]
)
def test_guard_refuses(ref):
    with pytest.raises(ForgejoRefused):
        _guard_ref(ref)


def test_guard_accepts():
    assert _guard_ref("bot/promote/abc") == "bot/promote/abc"


async def test_last_commit():
    c, seen = make(lambda r: js([{"sha": "c1"}]))
    assert await c.last_commit("r", "agent/x.md", "main") == "c1"
    q = dict(seen[0].url.params)
    assert seen[0].url.path == "/api/v1/repos/kh/r/commits"
    assert q == {"path": "agent/x.md", "sha": "main", "limit": "1", "stat": "false",
                 "verification": "false", "files": "false"}
    c, _ = make(lambda r: js([]))
    assert await c.last_commit("r", "agent/x.md", "main") is None
    c, _ = make(lambda r: js({}, 500))
    with pytest.raises(ForgejoError, match="HTTP 500"):
        await c.last_commit("r", "p", "main")


async def test_blob_sha():
    def h(r):
        assert r.url.params["ref"] == "main"
        return js({"sha": "b1"}) if r.url.path.endswith("/a.md") else js({}, 404)

    c, _ = make(h)
    assert await c.blob_sha("r", "a.md", "main") == "b1"
    assert await c.blob_sha("r", "b.md", "main") is None


async def test_pull_for_head():
    c, seen = make(lambda r: js({"number": 4}))
    assert await c.pull_for_head("r", "bot/propose/x") == {"number": 4}
    assert seen[0].url.raw_path == b"/api/v1/repos/kh/r/pulls/main/bot/propose/x"
    c, _ = make(lambda r: js({}, 404))
    assert await c.pull_for_head("r", "bot/propose/x") is None
    c, _ = make(lambda r: js({}, 500))
    with pytest.raises(ForgejoError, match="HTTP 500"):
        await c.pull_for_head("r", "bot/propose/x")


async def test_org_member():
    codes = {"alice": 204, "bob": 404, "eve": 500}
    c, seen = make(lambda r: httpx.Response(codes[r.url.path.rsplit("/", 1)[1]]))
    assert await c.org_member("alice") is True
    assert await c.org_member("bob") is False
    with pytest.raises(ForgejoError, match="HTTP 500"):
        await c.org_member("eve")
    assert seen[0].url.path == "/api/v1/orgs/kh/members/alice"
