"""``sync_once`` as ``kh_indexer`` against the stored snapshot row."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.config import DirectoryConfig, GroupConfig, GroupDirectoryConfig, KhConfig
from knowledge_hub.directory import store, sync
from knowledge_hub.directory.model import DirectoryUser, GroupFetch
from knowledge_hub.directory.provider import ProviderError
from tests.support.pg import FRESH_DB


def config():
    return KhConfig(
        people={"sam": "sam@x.org"},
        groups={
            "g": GroupConfig(
                members=["sam"],
                approvers=["sam"],
                directory={"fake": GroupDirectoryConfig(members=["m"], approvers=[])},
            )
        },
        directory=DirectoryConfig(provider="fake", domains=["x.org"], refresh_seconds=60),
    )


def fetch_of(count):
    users = tuple(DirectoryUser(f"u{i}@x.org", True, True, f"u{i}") for i in range(count))
    return {"g": GroupFetch(members=users, approvers=())}


class Provider:
    name = "fake"

    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    async def fetch(self, refs):
        if self.error:
            raise self.error
        return self.result

    async def aclose(self):
        pass


def fetched_at(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        return conn.execute("SELECT fetched_at FROM kh.directory_snapshot").fetchone()[0]


async def test_good_run_moves_fetched_at_and_failures_do_not(indexer_pool, db_settings, fresh_db):
    cfg = config()
    await sync.sync_once(cfg, db_settings, Provider(fetch_of(10)), indexer_pool)
    first = fetched_at(fresh_db)

    with pytest.raises(sync.ChangeRefused):
        await sync.sync_once(cfg, db_settings, Provider(fetch_of(1)), indexer_pool)
    assert fetched_at(fresh_db) == first

    with pytest.raises(ProviderError):
        await sync.sync_once(cfg, db_settings, Provider(error=ProviderError("boom")), indexer_pool)
    assert fetched_at(fresh_db) == first

    await sync.sync_once(cfg, db_settings, Provider(fetch_of(10)), indexer_pool)
    assert fetched_at(fresh_db) > first

    await sync.sync_once(cfg, db_settings, Provider(fetch_of(1)), indexer_pool, accept_change=True)
    async with indexer_pool.connection() as conn:
        data, _ = await store.load_snapshot(conn)
    assert len(data["groups"]["g"]["members"]) == 1


async def test_daily_prints_identity_rows_purged(indexer_pool, db_settings, capsys):
    from types import SimpleNamespace

    from knowledge_hub.cli import _daily

    deps = SimpleNamespace(indexer_pool=indexer_pool, settings=db_settings)
    assert await _daily(deps) == 0
    out = capsys.readouterr().out.strip()
    assert "; identity rows purged: 0; usage events purged: " in out
