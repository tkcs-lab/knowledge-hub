import re

import pytest

from knowledge_hub.config import parse_config
from knowledge_hub.directory.model import Membership
from knowledge_hub.forgejo.admin import ForgejoAdmin
from knowledge_hub.setup.forgejo import ForgejoStep
from knowledge_hub.setup.steps import run
from tests.unit.test_setup_forgejo import Fake

pytestmark = pytest.mark.unit

BASE = {
    "org": "kh",
    "hub_url": "http://hub:8080",
    "people": {"alice": "alice@example.com"},
    "groups": {"all": {"members": ["alice"], "approvers": ["alice"]}},
}
WITH_PROVIDER = {
    **BASE,
    "directory": {"provider": "google", "domains": ["example.com"], "max_stale_seconds": 600},
}


def effective(*, members=("alice", "carol"), approvers=("alice", "carol")):
    return Membership(
        {"alice": "alice@example.com", "carol": "carol@example.com"},
        {"all": frozenset(members)},
        {"all": frozenset(approvers)},
    )


@pytest.fixture
def fake():
    f = Fake(org="kh", token="admin-tok")
    for login in ("alice", "kh-bot", "kh-indexer"):
        f.add_user(login)
    return f


@pytest.fixture
async def admin(fake):
    a = ForgejoAdmin("https://forgejo.test", "admin-tok", "kh", transport=fake.transport)
    yield a
    await a.aclose()


def make(admin, config, loader):
    return ForgejoStep(
        parse_config(config), admin, webhook_secret=None, bot_token=None, indexer_token=None,
        membership_loader=loader,
    )


def loader(membership, age=None):
    calls = []

    async def load():
        calls.append(1)
        return membership, age

    load.calls = calls
    return load


def lines(changes):
    return [c.render() for c in changes]


def has(changes, prefix, pattern):
    return any(c.prefix == prefix and re.search(pattern, c.text) for c in changes)


def members_of(fake, name):
    return sorted(fake.team_members[fake.team_id(name)])


async def test_directory_members_get_teams(fake, admin):
    fake.add_user("carol")
    s = make(admin, WITH_PROVIDER, loader(effective(), 10))
    await s.apply(prune=False)
    assert members_of(fake, "all") == ["alice", "carol"]
    assert members_of(fake, "all-approve") == ["alice", "carol"]
    assert has(await s.plan(), "i", r"^directory: 2 people from the directory snapshot$")


async def test_directory_approver_only_in_approve_team(fake, admin):
    fake.add_user("carol")
    s = make(admin, WITH_PROVIDER, loader(effective(approvers=("carol",)), 10))
    await s.apply(prune=False)
    assert members_of(fake, "all-approve") == ["carol"]


async def test_directory_person_without_account(fake, admin):
    s = make(admin, WITH_PROVIDER, loader(effective(), 10))
    changes = await s.apply(prune=False)
    assert "i account carol: no Forgejo account yet; not added to its teams" in lines(changes)
    assert members_of(fake, "all") == ["alice"]


async def test_directory_person_with_other_email(fake, admin):
    fake.add_user("carol", email="someone.else@example.com")
    s = make(admin, WITH_PROVIDER, loader(effective(), 10))
    changes = await s.apply(prune=False)
    assert "! account carol: email differs from directory; not added" in lines(changes)
    assert members_of(fake, "all") == ["alice"]


async def test_existing_member_whose_email_changed_is_removed(fake, admin):
    fake.add_user("carol")
    await make(admin, WITH_PROVIDER, loader(effective(), 10)).apply(prune=False)
    assert members_of(fake, "all") == ["alice", "carol"]
    fake.add_user("carol", email="someone.else@example.com")
    await make(admin, WITH_PROVIDER, loader(effective(), 10)).apply(prune=False)
    assert members_of(fake, "all") == ["alice"]
    assert members_of(fake, "all-approve") == ["alice"]


async def test_directory_email_compare_is_case_insensitive(fake, admin):
    fake.add_user("carol", email="Carol@Example.com")
    await make(admin, WITH_PROVIDER, loader(effective(), 10)).apply(prune=False)
    assert members_of(fake, "all") == ["alice", "carol"]


async def test_static_person_without_account_keeps_bang(fake, admin):
    fake.users.discard("alice")
    changes = await make(admin, WITH_PROVIDER, loader(effective(), 10)).plan()
    assert "! account alice: does not exist" in lines(changes)


async def test_setup_plan_before_0006_with_provider_prints_snapshot_missing(fake, admin):
    static = Membership.from_config(parse_config(WITH_PROVIDER))
    changes = await make(admin, WITH_PROVIDER, loader(static, None)).plan()
    assert "! directory: snapshot missing" in lines(changes)
    assert await run([make(admin, WITH_PROVIDER, loader(static, None))], "plan") == 1


async def test_stale_snapshot_prints_age(fake, admin):
    fake.add_user("carol")
    changes = await make(admin, WITH_PROVIDER, loader(effective(), 900)).plan()
    assert "! directory: snapshot is 900s old (limit 600s)" in lines(changes)


async def test_stale_snapshot_leaves_directory_people_out(fake, admin):
    fake.add_user("carol")
    static = Membership.from_config(parse_config(WITH_PROVIDER))
    s = make(admin, WITH_PROVIDER, loader(static, 900))
    changes = await s.apply(prune=False)
    assert members_of(fake, "all") == ["alice"]
    assert "! directory: snapshot is 900s old (limit 600s)" in lines(changes)


async def test_provider_none_prints_nothing_new(fake, admin):
    s = make(admin, BASE, loader(Membership.from_config(parse_config(BASE)), None))
    changes = await s.plan()
    assert not any("directory" in c.text for c in changes)
    assert s.directory_changes() == []


async def test_no_loader_uses_static_membership(fake, admin):
    s = make(admin, BASE, None)
    await s.apply(prune=False)
    assert members_of(fake, "all") == ["alice"]


async def test_loader_runs_once_per_call(fake, admin):
    load = loader(Membership.from_config(parse_config(BASE)))
    s = make(admin, BASE, load)
    await s.plan()
    await s.apply(prune=False)
    await s.sync_group("all", write=False)
    assert len(load.calls) == 3


def test_cli_builder_does_not_load_a_snapshot(monkeypatch, tmp_path):
    from knowledge_hub.directory import store
    from knowledge_hub.settings import Settings
    from knowledge_hub.setup.cli import build_steps

    token = tmp_path / "tok"
    token.write_text("t")
    called = []

    async def boom(config, settings):
        called.append(1)
        raise AssertionError("loaded at build time")

    monkeypatch.setattr(store, "load_membership", boom)
    settings = Settings.model_construct(
        forgejo_url="http://f", setup_forgejo_admin_token_file=token, webhook_hmac_file=None,
        forgejo_bot_token_file=None, forgejo_indexer_token_file=None, setup_slack_webhooks_file=None,
    )
    step = build_steps(settings, parse_config(BASE), False)["forgejo"]()
    assert step._loader is not None and called == []
