import asyncio

from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake, pr


def run(coro):
    return asyncio.run(coro)


def test_pr_without_new_arguments_is_unchanged():
    assert pr(3, "m", "h") == {
        "number": 3,
        "html_url": "https://forgejo.test/kh/repo/pulls/3",
        "merged": True,
        "merge_commit_sha": "m",
        "user": {"login": "alice"},
        "base": {"ref": "main"},
        "head": {"sha": "h"},
    }


def test_pr_with_merger_fields():
    got = pr(3, "m", "h", merged_by="bob", merged_at="2026-11-02T10:00:00Z", author_id=7,
             merged_by_id=8)
    assert got["merged_by"] == {"login": "bob", "id": 8}
    assert got["user"] == {"login": "alice", "id": 7}
    assert got["merged_at"] == "2026-11-02T10:00:00Z"
    assert pr(3, "m", "h", merged_by="bob")["merged_by"] == {"login": "bob"}


def test_teams():
    fake = ForgejoFake({}, teams={"a": {"x"}, "b": {"y", "z"}})
    assert run(fake.team_id("a")) == 1
    assert run(fake.team_id("b")) == 2
    assert run(fake.team_id("c")) is None
    assert run(fake.is_team_member(2, "z")) is True
    assert run(fake.is_team_member(1, "z")) is False
    assert fake.calls == ["team_id"] * 3 + ["is_team_member"] * 2
    assert run(ForgejoFake({}).team_id("a")) is None


def _shas(fake, sha):
    return {e["path"]: e["sha"] for e in run(fake.list_tree("r", sha))}


def test_list_tree_sha_follows_content():
    repo = FakeRepo(commits=[
        FakeCommit("c1", files={"a.md": "same", "b.md": "same"}),
        FakeCommit("c2", ["c1"], files={"b.md": "edited"}),
        FakeCommit("c3", ["c2"], files={"0.md": "new"}),
    ])
    fake = ForgejoFake({"r": repo})
    one, two, three = _shas(fake, "c1"), _shas(fake, "c2"), _shas(fake, "c3")
    assert one["a.md"] == one["b.md"]
    assert two["b.md"] != one["b.md"]
    assert two["a.md"] == one["a.md"]
    assert three["a.md"] == one["a.md"] and three["b.md"] == two["b.md"]
