"""Snapshot store, ``MembershipCache`` and ``load_membership`` against a real database."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from knowledge_hub.config import DirectoryConfig, GroupConfig, KhConfig
from knowledge_hub.db.migrate import migrate
from knowledge_hub.directory import store
from knowledge_hub.directory.model import DirectoryUser, GroupFetch, Membership
from knowledge_hub.metrics import Metrics
from knowledge_hub.settings import Settings
from tests.support.pg import FRESH_DB

FETCHED = {"g": GroupFetch(members=(DirectoryUser("ann@x.org", True, True, "ann"),), approvers=())}
MAX_STALE = 600


def config(provider="keycloak"):
    return KhConfig(
        people={"sam": "sam@x.org"},
        groups={"g": GroupConfig(members=["sam"], approvers=["sam"])},
        directory=DirectoryConfig(
            provider=provider, domains=["x.org"], refresh_seconds=60, max_stale_seconds=MAX_STALE
        ),
    )


def snapshot(provider="keycloak"):
    return store.fetched_to_snapshot(provider, FETCHED)


async def put(indexer_pool, data):
    async with indexer_pool.connection() as conn:
        await store.put_snapshot(conn, data)
        await conn.commit()


def fetched_at_epoch(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        return conn.execute("SELECT fetched_at FROM kh.directory_snapshot").fetchone()[0].timestamp()


def static(cfg):
    return Membership.from_config(cfg)


async def test_put_then_load_round_trip(indexer_pool):
    assert (await _load(indexer_pool)) is None
    await put(indexer_pool, snapshot())
    data, fetched_at = await _load(indexer_pool)
    assert data == snapshot()
    assert store.snapshot_to_fetched(data) == FETCHED
    assert abs((datetime.now(UTC) - fetched_at).total_seconds()) < 60


async def _load(pool):
    async with pool.connection() as conn:
        return await store.load_snapshot(conn)


async def test_app_can_get_but_not_put(app_pool, indexer_pool):
    await put(indexer_pool, snapshot())
    assert (await _load(app_pool)) is not None
    async with app_pool.connection() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await store.put_snapshot(conn, snapshot())


async def test_current_goes_static_after_max_stale_without_refresh(
    app_pool, indexer_pool, db_settings, fresh_db
):
    await put(indexer_pool, snapshot())
    at = fetched_at_epoch(fresh_db)
    clock = [at + 10]
    cfg = config()
    cache = store.MembershipCache(app_pool, cfg, db_settings, now=lambda: clock[0])
    await cache.refresh()
    assert "ann" in cache.current().members["g"]
    clock[0] = at + MAX_STALE + 1
    assert cache.current() == static(cfg)
    assert cache.age_seconds() == pytest.approx(MAX_STALE + 1)


async def test_refresh_db_error_does_not_extend_freshness(app_pool, indexer_pool, db_settings, fresh_db):
    await put(indexer_pool, snapshot())
    at = fetched_at_epoch(fresh_db)
    clock = [at + 10]
    cfg = config()
    cache = store.MembershipCache(app_pool, cfg, db_settings, now=lambda: clock[0])
    await cache.refresh()
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        conn.execute("DROP FUNCTION kh.directory_snapshot_get()")
    clock[0] = at + 300
    await cache.refresh()  # fails: logged, previous membership kept
    assert "ann" in cache.current().members["g"]
    clock[0] = at + MAX_STALE + 1
    await cache.refresh()
    assert cache.current() == static(cfg)


async def test_no_row_with_provider_set_is_static(app_pool, db_settings):
    cfg = config()
    cache = store.MembershipCache(app_pool, cfg, db_settings)
    await cache.refresh()
    assert cache.current() == static(cfg) and cache.age_seconds() is None


async def test_provider_none_ignores_stored_snapshot(app_pool, indexer_pool, db_settings):
    await put(indexer_pool, snapshot("none"))
    cfg = config("none")
    cache = store.MembershipCache(app_pool, cfg, db_settings)
    await cache.refresh()
    assert cache.current() == static(cfg)
    assert await store.load_membership(cfg, db_settings) == (static(cfg), None)


async def test_snapshot_of_other_provider_is_treated_as_missing(app_pool, indexer_pool, db_settings):
    await put(indexer_pool, snapshot("google"))
    cfg = config("keycloak")
    cache = store.MembershipCache(app_pool, cfg, db_settings)
    await cache.refresh()
    assert cache.current() == static(cfg) and cache.age_seconds() is None
    membership, age = await store.load_membership(cfg, db_settings)
    assert membership == static(cfg) and age is not None


async def test_load_membership_returns_age_and_membership(indexer_pool, db_settings):
    cfg = config()
    assert await store.load_membership(cfg, db_settings) == (static(cfg), None)
    await put(indexer_pool, snapshot())
    membership, age = await store.load_membership(cfg, db_settings)
    assert "ann" in membership.members["g"]
    assert age is not None and 0 <= age < 60


async def test_load_membership_static_when_function_missing(fresh_db):
    """A database migrated only to 0005 has neither the table nor the function."""
    with psycopg.connect(fresh_db.admin_dsn(), autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS kh_old WITH (FORCE)")
        conn.execute("CREATE DATABASE kh_old OWNER kh_migrator")
    with psycopg.connect(fresh_db.admin_dsn("kh_old"), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    migrate(fresh_db.dsn("kh_migrator", "kh_old"), stop_after="0005_move_record.sql")
    settings = Settings(db_host=str(fresh_db.sock), db_port=5432, db_name="kh_old")
    cfg = config()
    assert await store.load_membership(cfg, settings) == (static(cfg), None)


async def test_gauge_set_by_refresh(app_pool, indexer_pool, db_settings, monkeypatch):
    fresh = Metrics()
    monkeypatch.setattr(store, "METRICS", fresh)
    assert "kh_directory_snapshot_age_seconds" not in fresh.render()
    await put(indexer_pool, snapshot())
    cache = store.MembershipCache(app_pool, config(), db_settings)
    await cache.refresh()
    assert "kh_directory_snapshot_age_seconds" in fresh.render()
