import pytest

from knowledge_hub.forgejo import ForgejoError
from knowledge_hub.forgejo.admin import ForgejoAdmin
from tests.support.forgejo_admin_fake import FakeForgejoAdmin

PROT = {
    "rule_name": "main", "enable_push": False, "enable_push_whitelist": False,
    "enable_merge_whitelist": True, "merge_whitelist_teams": ["g-approve"],
    "required_approvals": 0, "block_on_rejected_reviews": True, "apply_to_admins": True,
}


@pytest.fixture
def fake():
    f = FakeForgejoAdmin(org="kh", token="admin-secret")
    f.add_user("alice")
    f.add_user("kh-bot", token="bot-tok")
    return f


@pytest.fixture
async def admin(fake):
    a = ForgejoAdmin("https://forgejo.test", "admin-secret", "kh", transport=fake.transport)
    yield a
    await a.aclose()


async def test_team_crud_and_members(fake, admin):
    team = await admin.create_team("g", "write", "group g", units_map={"repo.code": "write"})
    assert team["name"] == "g" and team["units_map"] == {"repo.code": "write"}
    names = [t["name"] for t in await admin.org_teams()]
    assert names == ["Owners", "g"]
    edited = await admin.edit_team(team["id"], permission="read", can_create_org_repo=False)
    assert edited["permission"] == "read"
    await admin.add_team_member(team["id"], "alice")
    assert [u["login"] for u in await admin.team_members(team["id"])] == ["alice"]
    await admin.remove_team_member(team["id"], "alice")
    assert await admin.team_members(team["id"]) == []


async def test_team_unknown_user_is_an_error(admin):
    team = await admin.create_team("g", "write", "")
    with pytest.raises(ForgejoError, match="HTTP 404"):
        await admin.add_team_member(team["id"], "nobody")


async def test_repo_lifecycle(fake, admin):
    assert await admin.get_repo("kb-g") is None
    created = await admin.create_repo("kb-g")
    assert created["private"] is True and created["default_branch"] == "main" and created["empty"] is False
    assert (await admin.get_repo("kb-g"))["name"] == "kb-g"
    assert [r["name"] for r in await admin.repos()] == ["kb-g"]
    edited = await admin.edit_repo("kb-g", has_actions=False, default_delete_branch_after_merge=True)
    assert edited["has_actions"] is False and edited["default_delete_branch_after_merge"] is True
    assert fake.bodies[0] == {"name": "kb-g", "private": True, "default_branch": "main", "auto_init": True}


async def test_team_repos(fake, admin):
    await admin.create_repo("kb-g")
    team = await admin.create_team("g", "write", "")
    assert await admin.team_repos(team["id"]) == []
    await admin.add_team_repo(team["id"], "kb-g")
    assert [r["name"] for r in await admin.team_repos(team["id"])] == ["kb-g"]


async def test_collaborators_and_permission(fake, admin):
    await admin.create_repo("kb-g")
    await admin.put_collaborator("kb-g", "kh-bot", "write")
    assert [u["login"] for u in await admin.collaborators("kb-g")] == ["kh-bot"]
    assert (await admin.user_repo_permission("kb-g", "kh-bot"))["permission"] == "write"
    assert (await admin.user_repo_permission("kb-g", "alice"))["permission"] == "none"
    team = await admin.create_team("g", "read", "")
    await admin.add_team_member(team["id"], "alice")
    await admin.add_team_repo(team["id"], "kb-g")
    assert (await admin.user_repo_permission("kb-g", "alice"))["permission"] == "read"


async def test_protection_create_then_edit_by_rule_name(fake, admin):
    await admin.create_repo("kb-g")
    team = await admin.create_team("g-approve", "read", "")
    await admin.add_team_repo(team["id"], "kb-g")
    assert await admin.protection("kb-g") is None
    await admin.put_protection("kb-g", PROT)
    assert list(fake.protections["kb-g"]) == ["main"]
    await admin.put_protection("kb-g", {**PROT, "required_approvals": 1})
    stored = await admin.protection("kb-g")
    assert stored["required_approvals"] == 1 and stored["merge_whitelist_teams"] == ["g-approve"]
    assert [m for m, p in fake.requests if "branch_protections" in p and m in ("POST", "PATCH")] == [
        "POST", "PATCH",
    ]


async def test_protection_needs_rule_name(admin):
    with pytest.raises(ForgejoError, match="no rule_name"):
        await admin.put_protection("kb-g", {"enable_push": False})


async def test_whitelist_team_without_read_access_is_silently_dropped(fake, admin):
    await admin.create_repo("kb-g")
    await admin.create_team("g-approve", "read", "")  # exists, but not added to the repo
    written = await admin.put_protection("kb-g", PROT)
    assert written["merge_whitelist_teams"] == []
    assert (await admin.protection("kb-g"))["merge_whitelist_teams"] == []
    # adding the team to the repo first keeps it
    tid = fake.team_id("g-approve")
    await admin.add_team_repo(tid, "kb-g")
    await admin.put_protection("kb-g", PROT)
    assert (await admin.protection("kb-g"))["merge_whitelist_teams"] == ["g-approve"]


async def test_whitelist_unknown_team_is_422(admin):
    await admin.create_repo("kb-g")
    with pytest.raises(ForgejoError, match="HTTP 422"):
        await admin.put_protection("kb-g", {**PROT, "merge_whitelist_teams": ["ghost"]})


async def test_org_hooks(fake, admin):
    assert await admin.org_hooks() == []
    body = {"type": "forgejo", "config": {"url": "https://hub/webhooks/forgejo", "content_type": "json",
                                           "secret": "s1"}, "events": ["push"], "active": True}
    hook = await admin.create_org_hook(body)
    assert [h["id"] for h in await admin.org_hooks()] == [hook["id"]]
    edited = await admin.edit_org_hook(hook["id"], {"config": {"secret": "s2"}, "active": False})
    assert edited["config"]["secret"] == "s2" and edited["active"] is False
    assert edited["config"]["url"] == "https://hub/webhooks/forgejo"


async def test_whoami_with_and_without_token(fake, admin):
    assert (await admin.whoami())["login"] == "kh-admin"
    me = await admin.whoami("bot-tok")
    assert me["login"] == "kh-bot" and me["is_admin"] is False
    with pytest.raises(ForgejoError, match="HTTP 401"):
        await admin.whoami("wrong")


async def test_org_owners(fake, admin):
    assert await admin.org_owners() == []
    await admin.add_team_member(fake.team_id("Owners"), "alice")
    assert [u["login"] for u in await admin.org_owners()] == ["alice"]


async def test_lists_are_paged(fake, admin):
    for i in range(120):
        fake.add_repo(f"r{i}")
    assert len(await admin.repos()) == 120
    assert sum(1 for m, p in fake.requests if p.endswith("/orgs/kh/repos")) == 3


async def test_errors_never_carry_the_token(fake):
    a = ForgejoAdmin("https://forgejo.test", "wrong-secret", "kh", transport=fake.transport)
    with pytest.raises(ForgejoError) as exc:
        await a.org_teams()
    assert "wrong-secret" not in str(exc.value) and "HTTP 401" in str(exc.value)
    await a.aclose()


async def test_non_admin_token_is_refused(fake):
    a = ForgejoAdmin("https://forgejo.test", "bot-tok", "kh", transport=fake.transport)
    with pytest.raises(ForgejoError, match="HTTP 403"):
        await a.repos()
    await a.aclose()


async def test_repo_hooks(fake, admin):
    await admin.create_repo("kb-g")
    assert await admin.repo_hooks("kb-g") == []
    body = {"type": "slack", "config": {"url": "https://hooks.slack.com/x", "content_type": "json",
                                         "channel": "#kh-g"}, "events": ["pull_request"], "active": True}
    hook = await admin.create_repo_hook("kb-g", body)
    assert [h["id"] for h in await admin.repo_hooks("kb-g")] == [hook["id"]]
    edited = await admin.edit_repo_hook("kb-g", hook["id"], {"active": False})
    assert edited["active"] is False and edited["config"]["url"] == "https://hooks.slack.com/x"
    await admin.delete_repo_hook("kb-g", hook["id"])
    assert await admin.repo_hooks("kb-g") == []
    nochannel = {**body, "config": {"url": "https://hooks.slack.com/x", "content_type": "json"}}
    with pytest.raises(ForgejoError, match="HTTP 422"):
        await admin.create_repo_hook("kb-g", nochannel)
