import pytest

from knowledge_hub.forgejo.client import ForgejoError
from knowledge_hub.merge_io import TeamDirectory, TeamLookupError, owners_at, tree_paths_changed

pytestmark = pytest.mark.unit

OWNERS_YML = "version: 1\naudience: all\ndomains: {}\nrules:\n  - {team: t, paths: ['**']}\n"


class Teams:
    def __init__(self, teams, fail=None):
        self.teams, self.fail, self.calls = teams, fail, []

    async def team_id(self, name):
        self.calls.append(("team_id", name))
        if self.fail:
            raise self.fail
        return list(self.teams).index(name) + 1 if name in self.teams else None

    async def is_team_member(self, team_id, login):
        self.calls.append(("is_team_member", team_id, login))
        return login in list(self.teams.values())[team_id - 1]


async def test_team_directory_caches():
    f = Teams({"a": {"ann"}, "b": set()})
    d = TeamDirectory(f)
    assert await d.is_member("a", "ann") and await d.is_member("a", "ann")
    assert await d.teams_of("ann", {"a", "b"}) == frozenset({"a"})
    assert [c for c in f.calls if c[0] == "team_id"] == [("team_id", "a"), ("team_id", "b")]
    assert len([c for c in f.calls if c[0] == "is_team_member"]) == 2


async def test_teams_of_empty_makes_no_call():
    f = Teams({})
    d = TeamDirectory(f)
    assert await d.teams_of("ann", None) == frozenset() == await d.teams_of("ann", set())
    assert f.calls == []


async def test_unknown_team():
    d = TeamDirectory(Teams({"a": set()}))
    with pytest.raises(TeamLookupError, match="team X does not exist in Forgejo"):
        await d.is_member("X", "ann")


async def test_forgejo_error_propagates():
    d = TeamDirectory(Teams({"a": set()}, fail=ForgejoError("GET /x failed: HTTP 403")))
    with pytest.raises(ForgejoError, match="HTTP 403"):
        await d.is_member("a", "ann")


class Raw:
    def __init__(self, text=None, exc=None):
        self.text, self.exc = text, exc

    async def get_raw(self, repo, path, sha):
        if self.exc:
            raise self.exc
        return self.text


async def test_owners_at_ok():
    o = await owners_at(Raw(OWNERS_YML), "r", "sha")
    assert o is not None and o.rules[0].team == "t"


async def test_owners_at_404_and_invalid_yaml_give_none():
    assert await owners_at(Raw(exc=ForgejoError("GET /raw failed: HTTP 404")), "r", "s") is None
    assert await owners_at(Raw("a: ["), "r", "s") is None


async def test_owners_at_500_propagates():
    with pytest.raises(ForgejoError):
        await owners_at(Raw(exc=ForgejoError("GET /raw failed: HTTP 500")), "r", "s")


class Trees:
    def __init__(self, trees):
        self.trees = trees

    async def list_tree(self, repo, sha):
        return self.trees[sha]


def blob(path, sha):
    return {"path": path, "type": "blob", "sha": sha}


async def test_tree_paths_changed():
    base = [
        blob("same", "1"),
        blob("edit", "1"),
        blob("gone", "1"),
        blob("old", "2"),
        {"path": "d", "type": "tree", "sha": "x"},
    ]
    head = [
        blob("same", "1"),
        blob("edit", "9"),
        blob("new", "1"),
        blob("new2", "2"),
        {"path": "d", "type": "tree", "sha": "y"},
    ]
    got = await tree_paths_changed(Trees({"b": base, "h": head}), "r", "b", "h")
    assert got == ("edit", "gone", "new", "new2", "old")


async def test_rename_is_two_paths():
    got = await tree_paths_changed(Trees({"b": [blob("a.md", "1")], "h": [blob("b.md", "1")]}), "r", "b", "h")
    assert got == ("a.md", "b.md")
