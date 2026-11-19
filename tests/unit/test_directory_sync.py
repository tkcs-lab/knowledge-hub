"""``kh directory`` and the sync loop, with a fake provider and no database."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from knowledge_hub import cli
from knowledge_hub.config import DirectoryConfig, GroupConfig, GroupDirectoryConfig, KhConfig
from knowledge_hub.directory import cli as dcli
from knowledge_hub.directory import store, sync
from knowledge_hub.directory.model import DirectoryUser, GroupFetch, Membership
from knowledge_hub.directory.provider import ProviderError

pytestmark = pytest.mark.unit


def user(n, **kw):
    return DirectoryUser(
        email=f"u{n}@x.org", enabled=kw.get("enabled", True), email_verified=True, username=f"u{n}"
    )


def fetch_of(count, approvers=0):
    users = tuple(user(i) for i in range(count))
    return {"g": GroupFetch(members=users, approvers=users[:approvers])}


def cfg(provider="fake", percent=50):
    return KhConfig(
        people={"sam": "sam@x.org"},
        groups={
            "g": GroupConfig(
                members=["sam"],
                approvers=["sam"],
                directory={"fake": GroupDirectoryConfig(members=["m"], approvers=["a"])},
            )
        },
        directory=DirectoryConfig(
            provider=provider, domains=["x.org"], refresh_seconds=60, shrink_guard_percent=percent
        ),
    )


class FakeProvider:
    name = "fake"

    def __init__(self, result=None, error=None):
        self.result, self.error, self.closed = result, error, False

    async def fetch(self, refs):
        if self.error:
            raise self.error
        return dict(self.result)

    async def aclose(self):
        self.closed = True


class FakePool:
    def __init__(self, stored=None):
        self.stored = stored  # snapshot dict or None
        self.puts = []

    @asynccontextmanager
    async def connection(self):
        yield self

    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield

        return tx()


@pytest.fixture(autouse=True)
def fake_store(monkeypatch):
    async def load(conn):
        return None if conn.stored is None else (conn.stored, datetime.now(UTC))

    async def put(conn, data):
        conn.puts.append(data)
        conn.stored = data

    monkeypatch.setattr(store, "load_snapshot", load)
    monkeypatch.setattr(store, "put_snapshot", put)


def stored_with(count):
    return store.fetched_to_snapshot("fake", fetch_of(count))


async def run(provider, pool, config=None, **kw):
    return await sync.sync_once(config or cfg(), None, provider, pool, **kw)


async def test_good_run_stores_and_logs(caplog):
    caplog.set_level(logging.INFO, logger="knowledge_hub.directory")
    pool = FakePool()
    users = (user(1), DirectoryUser("bad@other.org", True, True, "bad"))
    fetched = {"g": GroupFetch(members=users, approvers=users[:1])}
    result = await run(FakeProvider(fetched), pool)
    assert (result.users, result.groups, result.skips) == (2, 1, 1)
    text = caplog.text
    assert "directory: fake 2 users in 1 groups, 1 skipped" in text
    assert "directory: skip g member bad@other.org: domain not allowed" in text
    assert "directory: approvers added in g: u1" in text
    assert len(pool.puts) == 1


async def test_provider_error_keeps_old_snapshot():
    pool = FakePool(stored_with(3))
    old = pool.stored
    with pytest.raises(ProviderError):
        await run(FakeProvider(error=ProviderError("boom")), pool)
    assert pool.stored is old and pool.puts == []


async def test_fetch_missing_a_requested_group_is_not_stored():
    pool = FakePool()
    with pytest.raises(ProviderError):
        await run(FakeProvider({}), pool)
    assert pool.puts == []


async def test_change_guard_boundary():
    for new, ok in ((5, True), (4, False)):
        pool = FakePool(stored_with(10))
        if ok:
            await run(FakeProvider(fetch_of(new)), pool)
            assert len(pool.puts) == 1
        else:
            with pytest.raises(sync.ChangeRefused, match="10 users changed to 4; rerun with --accept-change"):
                await run(FakeProvider(fetch_of(new)), pool)
            assert pool.puts == []
    for new, ok in ((20, True), (21, False)):
        pool = FakePool(stored_with(10))
        if ok:
            await run(FakeProvider(fetch_of(new)), pool)
        else:
            with pytest.raises(sync.ChangeRefused):
                await run(FakeProvider(fetch_of(new)), pool)


async def test_small_stored_snapshot_has_no_growth_guard():
    pool = FakePool(stored_with(5))
    await run(FakeProvider(fetch_of(50)), pool)
    assert len(pool.puts) == 1


async def test_first_run_without_stored_snapshot_is_not_guarded():
    pool = FakePool()
    await run(FakeProvider(fetch_of(1000)), pool)
    assert len(pool.puts) == 1


async def test_accept_change_allows_refused_case():
    pool = FakePool(stored_with(10))
    await run(FakeProvider(fetch_of(1)), pool, accept_change=True)
    assert len(pool.puts) == 1


async def test_run_every_waits_and_logs_refusal(caplog):
    pool = FakePool(stored_with(10))
    waits = []

    class Stop(Exception):
        pass

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise Stop

    with pytest.raises(Stop):
        await sync.run_every(
            cfg(), None, FakeProvider(fetch_of(1)), pool, interval=77, accept_change=False, sleep=sleep
        )
    assert waits == [77, 77]
    refused = [r for r in caplog.records if "directory-refused" in r.getMessage()]
    assert len(refused) == 2 and all(r.levelno == logging.ERROR for r in refused)


async def test_run_every_returns_restart_code_when_watcher_fires():
    waits = []
    polls = iter([False, False, True])

    class Watcher:
        def poll(self):
            return next(polls)

    async def sleep(seconds):
        waits.append(seconds)

    code = await sync.run_every(
        cfg(), None, FakeProvider(fetch_of(1)), FakePool(), interval=5, accept_change=False,
        sleep=sleep, watcher=Watcher(),
    )  # fmt: skip
    assert code == 75 and waits == [5, 5, 5]


async def test_run_every_survives_provider_error(caplog):
    pool = FakePool()
    calls = []

    class Stop(Exception):
        pass

    async def sleep(seconds):
        calls.append(seconds)
        raise Stop

    with pytest.raises(Stop):
        await sync.run_every(
            cfg(), None, FakeProvider(error=ProviderError("x 500")), pool,
            interval=60, accept_change=False, sleep=sleep,
        )  # fmt: skip
    assert "directory: fake: x 500" in caplog.text and pool.puts == []


# --- command line ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in [k for k in __import__("os").environ if k.startswith("KH_")]:
        monkeypatch.delenv(k)

    def setup(config):
        monkeypatch.setattr(dcli, "load_settings", lambda: type("S", (), {"config_file": "x"})())
        monkeypatch.setattr(dcli, "load_config", lambda path: config)

    return setup


def membership_of(config):
    return Membership.from_config(config)


def stub_membership(monkeypatch, membership, age):
    async def load(config, settings):
        return membership, age

    monkeypatch.setattr(store, "load_membership", load)


def no_db(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("database touched")

    monkeypatch.setattr("knowledge_hub.db.pool.make_indexer_pool", boom)
    monkeypatch.setattr("knowledge_hub.directory.provider.build_provider", boom)


def test_unknown_provider_exits_2(env, monkeypatch, capsys):
    env(cfg("nope"))
    monkeypatch.setattr(
        "knowledge_hub.directory.provider.build_provider",
        lambda name, settings, **k: (_ for _ in ()).throw(ValueError(f"directory: unknown provider {name}")),
    )
    assert cli.main(["directory", "sync", "--once"]) == 2
    assert "directory: unknown provider nope" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["sync", "--once"], ["sync", "--every", "60"], ["sync"]])
def test_provider_none_sync_is_a_noop(env, monkeypatch, capsys, args):
    env(cfg("none"))
    no_db(monkeypatch)
    assert cli.main(["directory", *args]) == 0
    assert capsys.readouterr().out.strip() == "directory: provider none; nothing to sync"


def test_provider_none_reports_still_work(env, monkeypatch, capsys):
    config = cfg("none")
    env(config)
    stub_membership(monkeypatch, membership_of(config), None)
    assert cli.main(["directory", "plan"]) == 0
    assert "people: 1" in capsys.readouterr().out
    assert cli.main(["directory", "people", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {"login": "sam", "email": "sam@x.org", "source": "static", "groups": ["g"], "approver_of": ["g"]}
    ]
    assert cli.main(["directory", "show"]) == 0
    assert capsys.readouterr().out.splitlines() == ["provider: none", "snapshot: none"]


def test_plan_exits_1_without_snapshot_and_when_stale(env, monkeypatch, capsys):
    config = cfg()
    env(config)
    stub_membership(monkeypatch, membership_of(config), None)
    assert cli.main(["directory", "plan"]) == 1
    stub_membership(monkeypatch, membership_of(config), config.directory.max_stale_seconds + 1)
    assert cli.main(["directory", "plan"]) == 1
    stub_membership(monkeypatch, membership_of(config), 10)
    assert cli.main(["directory", "plan"]) == 0
    out = capsys.readouterr().out
    assert "groups: 1" in out and "skipped: 0" in out


def test_people_json_marks_directory_source(env, monkeypatch, capsys):
    config = cfg()
    env(config)
    base = membership_of(config)
    m = Membership(
        {**base.people, "ann": "ann@x.org"},
        {"g": frozenset({"sam", "ann"})},
        {"g": frozenset({"sam", "ann"})},
    )
    stub_membership(monkeypatch, m, 5)
    assert cli.main(["directory", "people", "--json"]) == 0
    rows = {r["login"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["ann"]["source"] == "directory" and rows["sam"]["source"] == "static"
    assert set(rows["ann"]) == {"login", "email", "source", "groups", "approver_of"}
    assert rows["ann"]["approver_of"] == ["g"]


def test_show_prints_age(env, monkeypatch, capsys):
    config = cfg()
    env(config)
    stub_membership(monkeypatch, membership_of(config), 12.7)
    assert cli.main(["directory", "show"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "provider: fake",
        "snapshot: 12s old, 1 users in 1 groups",
    ]


class _Pool:
    async def open(self):
        pass

    async def close(self):
        pass


def sync_env(env, monkeypatch, provider, config=None):
    env(config or cfg())
    monkeypatch.setattr(
        "knowledge_hub.directory.provider.build_provider", lambda name, settings, **k: provider
    )
    monkeypatch.setattr("knowledge_hub.db.pool.make_indexer_pool", lambda settings: _Pool())


def test_sync_exit_codes(env, monkeypatch, capsys):
    sync_env(env, monkeypatch, FakeProvider())

    async def fail(*a, **k):
        raise ProviderError("status 503")

    monkeypatch.setattr(sync, "sync_once", fail)
    assert cli.main(["directory", "sync", "--once"]) == 1
    assert "directory: fake: status 503" in capsys.readouterr().err

    async def refuse(*a, **k):
        raise sync.ChangeRefused(
            "directory: refusing snapshot: 10 users changed to 1; rerun with --accept-change"
        )

    monkeypatch.setattr(sync, "sync_once", refuse)
    assert cli.main(["directory", "sync", "--once"]) == 3
    assert "rerun with --accept-change" in capsys.readouterr().err

    async def ok(*a, accept_change, **k):
        assert accept_change is True
        return sync.SyncResult(1, 1, 0)

    monkeypatch.setattr(sync, "sync_once", ok)
    assert cli.main(["directory", "sync", "--once", "--accept-change"]) == 0


def test_every_out_of_range_is_a_usage_error(env):
    with pytest.raises(SystemExit) as exc:
        cli.main(["directory", "sync", "--every", "5"])
    assert exc.value.code == 2


class _FakePool:
    async def open(self):
        pass

    async def close(self):
        pass


class _FakeProvider:
    name = "fake"

    async def aclose(self):
        pass


@pytest.mark.parametrize(("seconds", "watched"), [(0, False), (10, True)])
def test_every_builds_a_watcher_only_when_config_watch_is_on(env, monkeypatch, seconds, watched):
    config = cfg("keycloak")
    monkeypatch.setattr(
        dcli,
        "load_settings",
        lambda: type("S", (), {"config_file": "x", "config_watch_seconds": seconds})(),
    )
    monkeypatch.setattr(dcli, "load_config", lambda path: config)
    monkeypatch.setattr("knowledge_hub.db.pool.make_indexer_pool", lambda settings: _FakePool())
    monkeypatch.setattr("knowledge_hub.directory.provider.build_provider", lambda n, s, **k: _FakeProvider())
    monkeypatch.setattr("knowledge_hub.config_watch.ConfigWatcher", lambda path, c: "watcher")
    seen = {}

    async def fake_run_every(*args, **kwargs):
        seen["watcher"] = kwargs["watcher"]
        return 0

    monkeypatch.setattr("knowledge_hub.directory.sync.run_every", fake_run_every)
    assert cli.main(["directory", "sync", "--every", "60"]) == 0
    assert seen["watcher"] == ("watcher" if watched else None)
