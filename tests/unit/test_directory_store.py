from datetime import UTC, datetime

import psycopg
import pytest

from knowledge_hub import metrics as metrics_mod
from knowledge_hub.config import DirectoryConfig, GroupConfig, KhConfig
from knowledge_hub.directory import store
from knowledge_hub.directory.model import DirectoryUser, GroupFetch, Membership
from knowledge_hub.metrics import Metrics
from knowledge_hub.settings import Settings

pytestmark = pytest.mark.unit


def user(email, enabled=True, verified=True, username=""):
    return DirectoryUser(email=email, enabled=enabled, email_verified=verified, username=username)


def config(provider="keycloak", max_stale=600):
    return KhConfig(
        people={"sam": "sam@x.org"},
        groups={"g": GroupConfig(members=["sam"], approvers=["sam"])},
        directory=DirectoryConfig(
            provider=provider, domains=["x.org"], refresh_seconds=60, max_stale_seconds=max_stale
        ),
    )


FETCHED = {"g": GroupFetch(members=(user("ann@x.org", username="ann"),), approvers=())}


def test_conversions_round_trip():
    snap = store.fetched_to_snapshot("keycloak", FETCHED)
    assert snap["version"] == 1 and snap["provider"] == "keycloak"
    assert store.snapshot_to_fetched(snap) == FETCHED


def test_row_without_enabled_is_not_enabled():
    data = {"groups": {"g": {"members": [{"email": "a@x.org", "email_verified": True}], "approvers": []}}}
    (u,) = store.snapshot_to_fetched(data)["g"].members
    assert u.enabled is False and u.email_verified is True
    data["groups"]["g"]["members"] = [{"email": "a@x.org", "enabled": "true", "email_verified": 1}]
    (u,) = store.snapshot_to_fetched(data)["g"].members
    assert (u.enabled, u.email_verified) == (False, False)


def test_wrong_shape_rows_are_skipped():
    rows = ["x", {"enabled": True}, {"email": "a@x.org"}]
    data = {"groups": {"g": {"members": rows, "approvers": 3}, "h": 1}}
    got = store.snapshot_to_fetched(data)
    assert [u.email for u in got["g"].members] == ["a@x.org"]
    assert got["g"].approvers == () and "h" not in got
    assert store.snapshot_to_fetched("junk") == {}


class FakeConn:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params=None):
        return self

    async def fetchone(self):
        return self.row


def patch_connect(monkeypatch, row):
    async def connect(dsn, **kw):
        return FakeConn(row)

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)


@pytest.mark.parametrize(
    "snapshot",
    [
        store.fetched_to_snapshot("keycloak", FETCHED),  # stale (below)
        store.fetched_to_snapshot("google", FETCHED),  # other provider
        {**store.fetched_to_snapshot("keycloak", FETCHED), "version": 2},
    ],
    ids=["stale", "other-provider", "version-2"],
)
async def test_load_membership_static_when_stale(monkeypatch, snapshot):
    cfg = config()
    fetched_at = datetime.fromtimestamp(0, UTC)
    if snapshot["provider"] == "keycloak" and snapshot["version"] == 1:
        pass  # epoch 0 is far older than max_stale_seconds
    else:
        fetched_at = datetime.now(UTC)
    patch_connect(monkeypatch, (snapshot, fetched_at))
    membership, age = await store.load_membership(cfg, Settings())
    assert membership == Membership.from_config(cfg)
    assert age is not None and age >= 0


async def test_load_membership_fresh_snapshot_applies(monkeypatch):
    cfg = config()
    patch_connect(monkeypatch, (store.fetched_to_snapshot("keycloak", FETCHED), datetime.now(UTC)))
    membership, age = await store.load_membership(cfg, Settings())
    assert "ann" in membership.members["g"] and age is not None and age < 60


async def test_load_membership_provider_none_opens_no_connection(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("connected")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", boom)
    cfg = config("none")
    assert await store.load_membership(cfg, Settings()) == (Membership.from_config(cfg), None)


class FakePool:
    def __init__(self, row):
        self.row = row

    def connection(self):
        if isinstance(self.row, Exception):
            raise self.row
        return FakeConn(self.row)


async def test_gauge_absent_until_refresh(monkeypatch):
    fresh = Metrics()
    monkeypatch.setattr(store, "METRICS", fresh)
    assert "kh_directory_snapshot_age_seconds" not in fresh.render()
    clock = [1000.0]
    snap = store.fetched_to_snapshot("keycloak", FETCHED)
    cache = store.MembershipCache(
        FakePool((snap, datetime.fromtimestamp(940, UTC))), config(), Settings(), now=lambda: clock[0]
    )
    await cache.refresh()
    assert "kh_directory_snapshot_age_seconds 60.0" in fresh.render()
    assert metrics_mod.METRICS is not fresh


async def test_cache_ages_without_refresh_and_survives_db_error(monkeypatch):
    monkeypatch.setattr(store, "METRICS", Metrics())
    clock = [1000.0]
    snap = store.fetched_to_snapshot("keycloak", FETCHED)
    pool = FakePool((snap, datetime.fromtimestamp(1000, UTC)))
    cache = store.MembershipCache(pool, config(max_stale=600), Settings(), now=lambda: clock[0])
    assert cache.current() == Membership.from_config(config())  # nothing adopted yet
    await cache.refresh()
    assert "ann" in cache.current().members["g"]
    pool.row = RuntimeError("db down")
    clock[0] = 1500.0
    await cache.refresh()
    assert "ann" in cache.current().members["g"]
    clock[0] = 1601.0
    await cache.refresh()
    assert cache.current() == Membership.from_config(config())
    assert cache.age_seconds() == pytest.approx(601.0)


async def test_cache_provider_none_never_reads_database():
    cache = store.MembershipCache(object(), config("none"), Settings())
    await cache.refresh()
    assert cache.current() == Membership.from_config(config("none")) and cache.age_seconds() is None
