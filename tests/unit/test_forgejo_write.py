import base64
import json

import httpx
import pytest

from knowledge_hub.forgejo import ForgejoClient, ForgejoError, ForgejoRefused
from knowledge_hub.forgejo.client import _guard_ref
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake

TOKEN = "s3cret-token"
BAD_REFS = [
    "main", "master", "bot/promote/", "bot/promote/../main", "bot/other/x", "feature/x", "",
    "refs/heads/main", "bot/propose/", "bot/propose/../main", "bot/propose//x",
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
    return httpx.Response(201, json={"number": 3, "commit": {"sha": "c9", "parents": [{"sha": "p1"}]}})


def test_guard_accepts_bot_ref_and_refuses_others():
    assert _guard_ref("bot/promote/x") == "bot/promote/x"
    assert _guard_ref("bot/propose/x") == "bot/propose/x"
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
    done = await c.put_file("r", "bot/promote/x", "product/a b.md", "héllo", "msg")
    assert done == {"sha": "c9", "parents": ["p1"]}
    pull = await c.open_pull("r", "bot/promote/x", "main", "T", "B", ["promotion", "domain/x"])
    assert pull["number"] == 3
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
        return httpx.Response(200, json={"commit": {"sha": "c2"}})

    c, seen = make(h)
    assert await c.put_file("r", "bot/promote/x", "a.md", "x", "m") == {"sha": "c2", "parents": []}
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


UUID = "123e4567-e89b-42d3-a456-426614174000"


def make_role(role, handler=ok, notes_repos=()):
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    c = ForgejoClient(
        "http://forgejo:3000/", TOKEN, "kh", transport=httpx.MockTransport(wrapped),
        role=role, notes_repos=notes_repos,
    )
    return c, seen


def test_guard_error_text():
    with pytest.raises(ForgejoRefused, match="bot/promote/ or bot/propose/"):
        _guard_ref("main")


@pytest.mark.parametrize("role", ["read", "notes"])
async def test_non_bot_roles_refuse_branch_and_status_writes(role):
    c, seen = make_role(role, notes_repos=["n"])
    assert c.role == role
    for call in (
        c.create_branch("r", "bot/propose/x", "abc"),
        c.put_file("r", "bot/propose/x", "a.md", "x", "m"),
        c.open_pull("r", "bot/propose/x", "main", "t", "b", []),
        c.post_status("r", "abc", "kh/validate", "success", "d"),
        c.post_requester("r", "abc", "alice", UUID),
    ):
        with pytest.raises(ForgejoRefused, match="cannot write branches or statuses"):
            await call
    assert seen == []


async def test_post_requester_body_and_guard():
    c, seen = make_role("bot")
    await c.post_requester("r", "abc", "alice", UUID)
    assert (seen[0].method, seen[0].url.path) == ("POST", "/api/v1/repos/kh/r/statuses/abc")
    assert json.loads(seen[0].content) == {
        "context": "kh/requested-by", "state": "success", "description": f"alice {UUID}"
    }
    for login, pid in [("Alice", UUID), ("alice\n", UUID), ("", UUID), ("a b", UUID),
                       ("alice", UUID + "\n"), ("alice", UUID.upper()), ("alice", "x"), ("-a", UUID),
                       ("a" * 40, UUID)]:
        with pytest.raises(ForgejoRefused):
            await c.post_requester("r", "abc", login, pid)
    assert len(seen) == 1


async def test_put_note_post_then_put_with_sha():
    c, seen = make_role("notes", notes_repos=["n"])
    assert await c.put_note("n", "agent/my-note.md", "héllo", "msg", None) == "c9"
    assert (seen[0].method, seen[0].url.path) == ("POST", "/api/v1/repos/kh/n/contents/agent/my-note.md")
    body = json.loads(seen[0].content)
    assert body["branch"] == "main" and body["message"] == "msg" and "sha" not in body
    assert base64.b64decode(body["content"]).decode() == "héllo"
    await c.put_note("n", "agent/my-note.md", "x", "msg", "blob1")
    assert seen[1].method == "PUT" and json.loads(seen[1].content)["sha"] == "blob1"


@pytest.mark.parametrize(
    "path",
    ["agent/x.md\n", "agent/x.md ", "agent/X.md", "agent/a/b.md", "agent/-x.md", "agent/x-.md",
     "agent/x--y.md", "agent/.md", "other/x.md", "agent/x.txt", "../agent/x.md", "agent/x.md/y", ""],
)
async def test_put_note_refuses_bad_paths(path):
    c, seen = make_role("notes", notes_repos=["n"])
    with pytest.raises(ForgejoRefused, match="not an agent note write"):
        await c.put_note("n", path, "x", "m", None)
    assert seen == []


async def test_put_note_refuses_repo_outside_notes_repos_and_other_roles():
    c, seen = make_role("notes", notes_repos=["n"])
    with pytest.raises(ForgejoRefused):
        await c.put_note("other", "agent/x.md", "x", "m", None)
    for role in ("bot", "read"):
        c2, seen2 = make_role(role, notes_repos=["n"])
        with pytest.raises(ForgejoRefused):
            await c2.put_note("n", "agent/x.md", "x", "m", None)
        assert seen2 == []
    assert seen == []


async def test_put_note_without_commit_sha_is_an_error():
    c, _ = make_role("notes", lambda r: httpx.Response(201, json={}), notes_repos=["n"])
    with pytest.raises(ForgejoError, match="commit response has no sha"):
        await c.put_note("n", "agent/x.md", "x", "m", None)
    c, _ = make_role("bot", lambda r: httpx.Response(404, json={}) if r.method == "GET"
                     else httpx.Response(201, json={}))
    with pytest.raises(ForgejoError, match="commit response has no sha"):
        await c.put_file("r", "bot/propose/x", "a.md", "x", "m")


async def test_fake_branch_commits_notes_and_reads():
    from knowledge_hub.forgejo.client import ForgejoError as FE

    fake = ForgejoFake({"r": FakeRepo([FakeCommit("c1", files={"product/a.md": "A"})])})
    await fake.create_branch("r", "bot/propose/x", "c1")
    done = await fake.put_file("r", "bot/propose/x", "product/b.md", "B", "m")
    assert done["parents"] == ["c1"]
    assert await fake.head_sha("r", "bot/propose/x") == done["sha"]
    assert await fake.head_sha("r", "main") == "c1"
    assert fake._repo("r").commits == [fake._repo("r").commits[0]]
    assert await fake.compare_files("r", "c1", done["sha"]) == [
        {"filename": "product/b.md", "status": "added"}
    ]
    pull = await fake.open_pull("r", "bot/propose/x", "main", "t", "b", [])
    assert pull["head"] == {"ref": "bot/propose/x", "sha": done["sha"]} and pull["state"] == "open"
    assert await fake.pull_for_head("r", "bot/propose/x") is pull
    assert await fake.pull_for_head("r", "nope") is None
    assert fake.messages == [("r", "bot/propose/x", "product/b.md", "m")]
    # notes
    assert await fake.blob_sha("r", "agent/n.md", "main") is None
    n1 = await fake.put_note("r", "agent/n.md", "one", "m1", None)
    blob = await fake.blob_sha("r", "agent/n.md", "main")
    assert len(blob) == 40 and await fake.head_sha("r", "main") == n1
    assert await fake.last_commit("r", "agent/n.md", "main") == n1
    assert await fake.last_commit("r", "product/a.md", "main") == "c1"
    with pytest.raises(FE, match="HTTP 422"):
        await fake.put_note("r", "agent/n.md", "two", "m", None)
    with pytest.raises(FE, match="HTTP 409"):
        await fake.put_note("r", "agent/n.md", "two", "m", "bad")
    fake.fail_after_commit = True
    with pytest.raises(FE, match="timeout"):
        await fake.put_note("r", "agent/n.md", "two", "m", blob)
    assert await fake.get_raw("r", "agent/n.md", await fake.head_sha("r", "main")) == "two"
    # statuses, membership, read-only
    await fake.post_requester("r", done["sha"], "alice", UUID)
    assert (await fake.commit_statuses("r", done["sha"]))[0]["description"] == f"alice {UUID}"
    fake.unknown_users.add("eve")
    assert await fake.org_member("alice") and not await fake.org_member("eve")
    ro = ForgejoFake(fake.repos, read_only=True)
    for call in (ro.put_note("r", "agent/n.md", "x", "m", None), ro.post_requester("r", "a", "b", UUID)):
        with pytest.raises(FE):
            await call
    assert ro.refused_writes == ["put_note", "post_requester"]
