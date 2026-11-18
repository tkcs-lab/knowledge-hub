import re

import httpx
import pytest

from knowledge_hub.config import parse_config
from knowledge_hub.forgejo.admin import ForgejoAdmin
from knowledge_hub.setup.forgejo import ForgejoStep, protection_body
from knowledge_hub.setup.steps import DRIFT
from tests.support.forgejo_admin_fake import FakeForgejoAdmin

CONFIG = {
    "org": "kh",
    "hub_url": "http://hub:8080",
    "people": {"alice": "alice@example.com", "bob": "bob@example.com"},
    "groups": {
        "all": {"members": ["alice", "bob"], "approvers": ["alice"]},
        "mgmt": {"repo": "kb-management", "members": ["alice"], "approvers": ["alice"]},
    },
}
URL = "http://hub:8080/webhooks/forgejo"


class Fake(FakeForgejoAdmin):
    """The p13 fake plus ``GET /users/{login}`` and ``DELETE /teams/{id}``, which the step also calls."""

    def _routes(self):
        return [
            (("GET", r"/users/([^/]+)"), self._user_get),
            (("DELETE", r"/teams/(\d+)"), self._team_delete),
            *super()._routes(),
        ]

    def _user_get(self, m, body, params):
        if m[1] not in self.users:
            return httpx.Response(404, json={"message": "no user"})
        return httpx.Response(200, json=self._user(m[1]))

    def _team_delete(self, m, body, params):
        tid = int(m[1])
        if tid not in self.teams:
            return httpx.Response(404, json={"message": "no team"})
        del self.teams[tid], self.team_members[tid], self.team_repos[tid]
        return httpx.Response(204)


@pytest.fixture
def fake():
    f = Fake(org="kh", token="admin-tok")
    for login in ("alice", "bob"):
        f.add_user(login)
    f.add_user("kh-bot", token="bot-tok")
    f.add_user("kh-indexer", token="idx-tok")
    return f


@pytest.fixture
async def admin(fake):
    a = ForgejoAdmin("https://forgejo.test", "admin-tok", "kh", transport=fake.transport)
    yield a
    await a.aclose()


def step(admin, config=None, *, secret="hmac", bot="bot-tok", indexer="idx-tok"):
    return ForgejoStep(parse_config(config or CONFIG), admin, webhook_secret=secret,
                       bot_token=bot, indexer_token=indexer)


def lines(changes):
    return [c.render() for c in changes]


def drift(changes):
    return [c.render() for c in changes if c.prefix in DRIFT]


def has(changes, prefix, pattern):
    return any(c.prefix == prefix and re.search(pattern, c.text) for c in changes)


@pytest.fixture
async def applied(fake, admin):
    s = step(admin)
    await s.apply(prune=False)
    assert drift(await s.plan()) == []
    return s


async def test_first_plan_is_only_creations(fake, admin):
    changes = await step(admin).plan()
    assert {c.prefix for c in changes} <= {"+", "i"}
    texts = [c.text for c in changes if c.prefix == "+"]
    for name in ("team all", "team all-approve", "repo kb-all", "team mgmt", "repo kb-management",
                 "protection kb-all/main", "protection kb-management/main", f"webhook {URL}",
                 "collaborator kb-all/kh-bot", "collaborator kb-all/kh-indexer",
                 "team repo all-approve: kb-all"):
        assert any(t.startswith(name) for t in texts), name
    assert "i webhook secret not verifiable" in lines(changes)
    # plan wrote nothing
    assert not any(m != "GET" for m, _ in fake.requests)
    assert len(fake.teams) == 1 and fake.repos == {} and fake.hooks == {}


async def test_apply_builds_the_state_of_3_7(fake, applied):
    for group, repo in (("all", "kb-all"), ("mgmt", "kb-management")):
        w, a = fake.teams[fake.team_id(group)], fake.teams[fake.team_id(f"{group}-approve")]
        assert w["permission"] == "write" and w["units_map"] == {"repo.code": "write", "repo.pulls": "write"}
        assert a["permission"] == "read" and a["units_map"] == {"repo.code": "read", "repo.pulls": "read"}
        for t in (w, a):
            assert t["includes_all_repositories"] is False and t["can_create_org_repo"] is False
            assert fake.team_repos[t["id"]] == [repo]
        r = fake.repos[repo]
        assert r["private"] and r["default_branch"] == "main" and not r["empty"]
        assert r["has_actions"] is False and r["default_delete_branch_after_merge"] is True
        assert fake.collabs[repo] == {"kh-bot": "write", "kh-indexer": "read"}
        assert fake.protections[repo]["main"] == protection_body(group)
    assert sorted(fake.team_members[fake.team_id("all")]) == ["alice", "bob"]
    assert fake.team_members[fake.team_id("all-approve")] == ["alice"]
    [hook] = fake.hooks.values()
    assert hook["type"] == "forgejo" and hook["events"] == ["push"] and hook["active"] is True
    assert hook["config"] == {"url": URL, "content_type": "json", "secret": "hmac"}


async def test_second_apply_rewrites_only_the_secret(fake, admin, applied):
    fake.requests.clear()
    changes = await step(admin, secret="new").apply(prune=False)
    assert drift(changes) == []
    writes = [(m, p) for m, p in fake.requests if m != "GET"]
    assert writes == [("PATCH", f"/api/v1/orgs/kh/hooks/{next(iter(fake.hooks))}")]
    assert next(iter(fake.hooks.values()))["config"]["secret"] == "new"


async def repaired(admin, prefix, pattern):
    s = step(admin)
    before = await s.plan()
    assert has(before, prefix, pattern), lines(before)
    done = await s.apply(prune=False)
    assert has(done, prefix, pattern), lines(done)
    assert not any(c.prefix == "!" for c in done), lines(done)
    after = await s.plan()
    assert drift(after) == [], lines(after)


async def test_member_added_by_hand(fake, admin, applied):
    fake.add_user("mallory")
    fake.team_members[fake.team_id("mgmt")].append("mallory")
    await repaired(admin, "~", r"^team mgmt: members -mallory")
    assert fake.team_members[fake.team_id("mgmt")] == ["alice"]


async def test_member_removed_by_hand(fake, admin, applied):
    fake.team_members[fake.team_id("all")].remove("bob")
    await repaired(admin, "~", r"^team all: members \+bob")


async def test_approver_removed(fake, admin, applied):
    fake.team_members[fake.team_id("all-approve")].clear()
    await repaired(admin, "~", r"^team all-approve: members \+alice")


@pytest.mark.parametrize("field,value", [
    ("permission", "admin"), ("units_map", {"repo.code": "admin"}),
    ("includes_all_repositories", True), ("can_create_org_repo", True),
])
async def test_team_field_changed(fake, admin, applied, field, value):
    fake.teams[fake.team_id("all-approve")][field] = value
    await repaired(admin, "~", rf"^team all-approve: {field}$")


@pytest.mark.parametrize("field,value", [
    ("private", False), ("default_branch", "dev"), ("has_actions", True),
    ("default_delete_branch_after_merge", False),
])
async def test_repo_setting_changed(fake, admin, applied, field, value):
    fake.repos["kb-all"][field] = value
    await repaired(admin, "~", rf"^repo kb-all: {field}$")


async def test_collaborator_missing(fake, admin, applied):
    del fake.collabs["kb-all"]["kh-indexer"]
    await repaired(admin, "+", r"^collaborator kb-all/kh-indexer")


async def test_collaborator_permission_raised(fake, admin, applied):
    fake.collabs["kb-all"]["kh-indexer"] = "write"
    await repaired(admin, "~", r"^collaborator kb-all/kh-indexer: permission write")
    assert fake.collabs["kb-all"]["kh-indexer"] == "read"


@pytest.mark.parametrize("field,value", [
    ("enable_push", True), ("enable_push_whitelist", True), ("enable_merge_whitelist", False),
    ("merge_whitelist_teams", ["all"]), ("required_approvals", 1),
    ("block_on_rejected_reviews", False), ("apply_to_admins", False),
])
async def test_protection_field_changed(fake, admin, applied, field, value):
    fake.protections["kb-all"]["main"][field] = value
    await repaired(admin, "~", rf"^protection kb-all/main: {field}$")


async def test_protection_deleted(fake, admin, applied):
    del fake.protections["kb-all"]["main"]
    await repaired(admin, "+", r"^protection kb-all/main$")


async def test_approve_team_removed_from_repo(fake, admin, applied):
    fake.team_repos[fake.team_id("all-approve")].clear()
    fake.protections["kb-all"]["main"]["merge_whitelist_teams"] = []  # what Forgejo would show
    s = step(admin)
    before = await s.plan()
    assert has(before, "+", r"^team repo all-approve: kb-all$")
    assert has(before, "~", r"^protection kb-all/main: merge_whitelist_teams$")
    await s.apply(prune=False)
    assert drift(await s.plan()) == []
    assert fake.protections["kb-all"]["main"]["merge_whitelist_teams"] == ["all-approve"]


async def test_silent_whitelist_drop_is_caught_by_the_read_back(fake, admin, applied, monkeypatch):
    fake.team_repos[fake.team_id("all-approve")].clear()
    fake.protections["kb-all"]["main"]["merge_whitelist_teams"] = []
    # the team add "succeeds" but grants no access, so Forgejo drops the team from the whitelist
    monkeypatch.setattr(fake, "_team_repo_add", lambda m, body, params: httpx.Response(204))
    done = await step(admin).apply(prune=False)
    assert has(done, "!", r"^protection kb-all/main: merge_whitelist_teams not kept")


async def test_extra_team_repo_is_refused(fake, admin, applied):
    fake.team_repos[fake.team_id("all")].append("kb-management")
    changes = await step(admin).apply(prune=False)
    assert has(changes, "!", r"^team repo all: kb-management is not the group repo")


async def test_webhook_missing(fake, admin, applied):
    fake.hooks.clear()
    await repaired(admin, "+", rf"^webhook {re.escape(URL)}$")
    assert next(iter(fake.hooks.values()))["config"]["secret"] == "hmac"


@pytest.mark.parametrize("field", ["events", "content_type", "active"])
async def test_webhook_field_changed(fake, admin, applied, field):
    hook = next(iter(fake.hooks.values()))
    if field == "events":
        hook["events"] = ["push", "pull_request"]
    elif field == "content_type":
        hook["config"]["content_type"] = "form"
    else:
        hook["active"] = False
    await repaired(admin, "~", rf"^webhook {re.escape(URL)}: {field}$")


async def test_without_hub_url(fake, admin):
    config = {**CONFIG, "hub_url": None}
    for changes in (await step(admin, config).plan(), await step(admin, config).apply(prune=False)):
        assert "! hub_url: not set" in lines(changes)
    assert fake.hooks == {}


async def test_apply_without_secret_refuses_the_hook(fake, admin):
    changes = await step(admin, secret=None).apply(prune=False)
    assert has(changes, "!", rf"^webhook {re.escape(URL)}: no secret")
    assert fake.hooks == {}


async def test_unmanaged_team_and_repo(fake, admin, applied):
    fake.add_team("old-team", "write")
    fake.add_repo("kb-old")
    changes = await step(admin).plan()
    assert "? unmanaged team old-team" in lines(changes)
    assert "? unmanaged repo kb-old" in lines(changes)
    assert "? unmanaged team Owners" not in lines(changes)
    assert drift(changes) == []
    kept = await step(admin).apply(prune=False)
    assert "? unmanaged team old-team" in lines(kept)


async def test_prune_removes_only_the_team(fake, admin, applied):
    fake.add_team("old-team", "write")
    fake.add_repo("kb-old")
    changes = await step(admin).apply(prune=True)
    assert "- team old-team" in lines(changes)
    assert "? unmanaged repo kb-old" in lines(changes)
    assert "old-team" not in {t["name"] for t in fake.teams.values()}
    assert "Owners" in {t["name"] for t in fake.teams.values()}
    assert "kb-old" in fake.repos
    assert not any(c.prefix == "-" and "team" not in c.text for c in changes)


async def test_identity_mismatch(fake, admin, applied):
    changes = await step(admin, bot="idx-tok").plan()
    assert has(changes, "!", r"^token kh-bot: belongs to kh-indexer$")
    assert has(await step(admin, bot="idx-tok").apply(prune=False), "!", r"^token kh-bot")


async def test_no_tokens_no_identity_calls(fake, admin, applied):
    fake.requests.clear()
    changes = await step(admin, bot=None, indexer=None).plan()
    assert drift(changes) == []
    assert ("GET", "/api/v1/user") not in fake.requests


async def test_bot_with_admin_permission(fake, admin, applied):
    tid = fake.add_team("ops", "admin", members=["kh-bot"])
    fake.team_repos[tid].append("kb-all")
    s = step(admin, {**CONFIG})
    plan = await s.plan()
    assert has(plan, "~", r"^collaborator kb-all/kh-bot: permission admin")
    done = await s.apply(prune=False)
    assert has(done, "!", r"^collaborator kb-all/kh-bot: permission admin after apply")


async def test_indexer_with_push(fake, admin, applied):
    fake.add_team("ops", "write", members=["kh-indexer"])
    fake.teams[fake.team_id("ops")]["includes_all_repositories"] = True
    done = await step(admin).apply(prune=False)
    assert has(done, "!", r"^collaborator kb-all/kh-indexer: permission write after apply")


async def test_bot_org_owner_and_site_admin(fake, admin, applied):
    fake.team_members[fake.team_id("Owners")].extend(["alice", "kh-bot"])
    fake.site_admins.add("kh-indexer")
    changes = await step(admin).plan()
    assert has(changes, "!", r"^account kh-bot: is an org owner$")
    assert has(changes, "!", r"^account kh-indexer: is a site admin$")
    assert has(changes, "!", r"^token kh-indexer: is a site admin$")
    assert "i owners: alice, kh-bot can bypass protection" in lines(changes)


async def test_missing_accounts(fake, admin):
    fake.users.discard("kh-indexer")
    fake.users.discard("bob")
    changes = await step(admin, indexer=None).apply(prune=False)
    assert "! account kh-indexer: does not exist" in lines(changes)
    assert "! account bob: does not exist" in lines(changes)
    assert fake.collabs["kb-all"] == {"kh-bot": "write"}
    assert fake.team_members[fake.team_id("all")] == ["alice"]
    plan = await step(admin, indexer=None).plan()
    assert [c.render() for c in plan if c.prefix in "+~-"] == []


async def test_new_group_leaves_existing_ones_alone(fake, admin, applied):
    config = {**CONFIG, "groups": {**CONFIG["groups"],
                                   "ops": {"members": ["bob"], "approvers": ["bob"]}}}
    fake.requests.clear()
    changes = await step(admin, config).apply(prune=False)
    assert all("ops" in c.text for c in changes if c.prefix in DRIFT), lines(changes)
    assert drift(await step(admin, config).plan()) == []
    writes = [p for m, p in fake.requests if m not in ("GET",)]
    assert not any("kb-all" in p or "kb-management" in p for p in writes)


def archived_config(**extra):
    groups = {**CONFIG["groups"], "mgmt": {**CONFIG["groups"]["mgmt"], "archived": True, **extra}}
    return {**CONFIG, "groups": groups}


async def test_archived_group_repo_is_archived_and_teams_untouched(fake, admin, applied):
    teams_before = {k: dict(v) for k, v in fake.teams.items()}
    members_before = {k: list(v) for k, v in fake.team_members.items()}
    protection = dict(fake.protections["kb-management"])
    changes = await step(admin, archived_config()).apply(prune=False)
    assert "~ repo kb-management: archived" in lines(changes)
    assert fake.repos["kb-management"]["archived"] is True
    assert fake.teams == teams_before and fake.team_members == members_before
    assert fake.protections["kb-management"] == protection
    assert fake.repos["kb-all"]["archived"] is False


async def test_archived_group_without_repo_creates_nothing(fake, admin):
    changes = await step(admin, archived_config()).apply(prune=False)
    assert "i group mgmt: archived, repo kb-management not created" in lines(changes)
    assert "kb-management" not in fake.repos
    assert not has(changes, "+", r"mgmt")


async def test_archived_group_is_converged_on_second_plan(fake, admin, applied):
    s = step(admin, archived_config())
    await s.apply(prune=False)
    again = await s.plan()
    assert not any(c.prefix in ("~", "+") for c in again), lines(again)


async def test_unarchived_group_unarchives_repo_before_protection(fake, admin, applied):
    await step(admin, archived_config()).apply(prune=False)
    assert fake.repos["kb-management"]["archived"] is True
    fake.requests.clear()
    changes = await step(admin).apply(prune=False)
    assert "~ repo kb-management: archived" in lines(changes)
    assert fake.repos["kb-management"]["archived"] is False
    patch = fake.requests.index(("PATCH", "/api/v1/repos/kh/kb-management"))
    prot = [i for i, (_, p) in enumerate(fake.requests) if "kb-management/branch_protections" in p]
    assert not prot or patch < min(prot)


async def test_archived_group_members_are_not_account_checked(fake, admin):
    config = archived_config(members=["ghost"], approvers=["ghost"])
    config = {**config, "people": {**config["people"], "ghost": "ghost@example.com"}}
    changes = await step(admin, config).plan()
    assert not any(c.prefix == "!" for c in changes), lines(changes)


# -- tech org (plan 3.8) --------------------------------------------------------------------------

TECH_CONFIG = {
    **CONFIG,
    "labels": {"ember": "Ember product"},
    "tech_org": "kh-tech",
    "tech_repos": {"docs-a": {"labels": ["ember"]}, "docs-b": {}},
}
TECH_URL = "http://hub:8080/webhooks/forgejo"


@pytest.fixture
def tech_fake():
    f = Fake(org="kh-tech", token="admin-tok")
    f.add_user("kh-indexer", token="idx-tok")
    return f


@pytest.fixture
async def tech_admin(tech_fake):
    a = ForgejoAdmin("https://forgejo.test", "admin-tok", "kh-tech", transport=tech_fake.transport)
    yield a
    await a.aclose()


def tech_step(admin, tech_admin, config=None, **kw):
    return ForgejoStep(parse_config(config or TECH_CONFIG), admin, webhook_secret="hmac",
                       bot_token="bot-tok", indexer_token="idx-tok", tech_admin=tech_admin, **kw)


async def test_tech_repo_missing_is_a_refusal_line(fake, admin, tech_fake, tech_admin):
    tech_fake.add_repo("docs-b")
    changes = await tech_step(admin, tech_admin).plan()
    assert "! tech repo docs-a: not in org kh-tech; create the clone or fork there" in lines(changes)
    assert not has(changes, "!", r"^tech repo docs-b")
    assert not any(m != "GET" for m, _ in tech_fake.requests)


async def test_tech_repo_present_gets_indexer_read_collaborator(fake, admin, tech_fake, tech_admin):
    for name in ("docs-a", "docs-b"):
        tech_fake.add_repo(name)
    s = tech_step(admin, tech_admin)
    assert "+ collaborator docs-a/kh-indexer: read" in lines(await s.plan())
    assert tech_fake.collabs["docs-a"] == {}
    await s.apply(prune=False)
    assert tech_fake.collabs["docs-a"] == {"kh-indexer": "read"}
    assert tech_fake.collabs["docs-b"] == {"kh-indexer": "read"}
    assert not any("kh-bot" in c for c in tech_fake.collabs.values())
    assert not has(await s.plan(), "+", r"^collaborator docs-")


async def test_tech_repo_line_lists_collaborators(fake, admin, tech_fake, tech_admin):
    for name in ("docs-a", "docs-b"):
        tech_fake.add_repo(name)
    tech_fake.add_user("zed")
    tech_fake.add_user("amy")
    tech_fake.collabs["docs-a"] = {"zed": "write", "amy": "read", "kh-indexer": "read"}
    out = lines(await tech_step(admin, tech_admin).plan())
    assert "i tech repo docs-a: collaborators amy, zed" in out
    assert "i tech repo docs-b: collaborators none" in out


async def test_unmanaged_tech_repo_is_reported(fake, admin, tech_fake, tech_admin):
    for name in ("docs-a", "docs-b", "stray"):
        tech_fake.add_repo(name)
    s = tech_step(admin, tech_admin)
    assert "? unmanaged tech repo stray" in lines(await s.plan())
    assert "? unmanaged tech repo stray" in lines(await s.apply(prune=True))
    assert "stray" in tech_fake.repos


async def test_tech_webhook_lines_have_the_tech_prefix(fake, admin, tech_fake, tech_admin):
    for name in ("docs-a", "docs-b"):
        tech_fake.add_repo(name)
    s = tech_step(admin, tech_admin)
    planned = lines(await s.plan())
    assert f"+ tech webhook {TECH_URL}" in planned
    assert "i tech webhook secret not verifiable" in planned
    await s.apply(prune=False)
    assert [h["config"]["url"] for h in tech_fake.hooks.values()] == [TECH_URL]
    assert drift(await s.plan()) == drift(await step(admin).plan())
    no_hub = {**TECH_CONFIG}
    no_hub.pop("hub_url")
    assert "! tech hub_url: not set" in lines(await tech_step(admin, tech_admin, no_hub).plan())


async def test_no_tech_repos_changes_no_output(fake, admin, tech_fake, tech_admin):
    plain = lines(await step(admin).plan())
    s = ForgejoStep(parse_config(CONFIG), admin, webhook_secret="hmac", bot_token="bot-tok",
                    indexer_token="idx-tok", tech_admin=None)
    assert lines(await s.plan()) == plain
    assert tech_fake.requests == []


async def test_tech_repos_never_get_teams_or_protection(fake, admin, tech_fake, tech_admin):
    for name in ("docs-a", "docs-b"):
        tech_fake.add_repo(name)
    await tech_step(admin, tech_admin).apply(prune=True)
    assert [t["name"] for t in tech_fake.teams.values()] == ["Owners"]
    assert tech_fake.protections == {"docs-a": {}, "docs-b": {}}
    assert tech_fake.repos["docs-a"] == tech_fake._new_repo("docs-a")
    bad = [(m, p) for m, p in tech_fake.requests
           if m != "GET" and "/collaborators/" not in p and "/hooks" not in p]
    assert bad == []
