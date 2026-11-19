"""Panel health checks against a real database."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import psycopg
import pytest

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.indexer.worker import MAX_ATTEMPTS
from knowledge_hub.panel import health
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.settings import PanelSettings
from tests.support.pg import FRESH_DB

pytestmark = pytest.mark.integration


def _config() -> KhConfig:
    return KhConfig(
        people={"alice": "alice@example.test"},
        groups={"all": GroupConfig(members=["alice"], approvers=["alice"])},
    )


@pytest.fixture
def ctx(fresh_db, indexer_pool, tmp_path):
    cfg_file = tmp_path / "kh.yaml"
    cfg_file.write_text("")
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all')")
        conn.execute("DELETE FROM kh.index_jobs")
    return PanelContext(
        settings=PanelSettings(env="dev", config_file=cfg_file, dev_admin="admin@example.test"),
        signer=None,
        upstream=None,
        load_config=_config,
        pool=indexer_pool,
        hub_settings=SimpleNamespace(db_migrator_password_file=None),
    )


def _admin(fresh_db):
    return psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True)


async def _checks(ctx) -> dict:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
    async with httpx.AsyncClient(transport=transport) as http:
        return {c["id"]: c for c in await health.run_checks(ctx, http=http)}


def _job(conn, state, attempts=0, age="0 minutes"):
    conn.execute(
        "INSERT INTO kh.index_jobs (kind, repo, payload, state, attempts, created_at)"
        " VALUES ('git_push', 'kb-all', '{}'::jsonb, %s, %s, now() - %s::interval)",
        (state, attempts, age),
    )


async def test_clean_database(ctx, fresh_db):
    with _admin(fresh_db) as conn:
        conn.execute("INSERT INTO kh.repo_state (repo, last_indexed_sha) VALUES ('kb-all', 'abc')")
    got = await _checks(ctx)
    assert got["database"]["status"] == "ok"
    assert got["queue"]["status"] == "ok"
    assert got["alerts"]["status"] == "ok"
    assert got["indexed"]["status"] == "ok"
    assert got["migrations"]["status"] == "n/a"


async def test_dead_job_fails_queue(ctx, fresh_db):
    with _admin(fresh_db) as conn:
        _job(conn, "failed", attempts=MAX_ATTEMPTS)
    assert (await _checks(ctx))["queue"]["status"] == "fail"


async def test_stuck_running_job_warns_queue(ctx, fresh_db):
    with _admin(fresh_db) as conn:
        _job(conn, "running", age="3 hours")
    assert (await _checks(ctx))["queue"]["status"] == "warn"


async def test_alert_warns(ctx, fresh_db):
    with _admin(fresh_db) as conn:
        conn.execute("INSERT INTO kh.index_alerts (repo, path, message) VALUES ('kb-all', 'a.md', 'bad')")
    assert (await _checks(ctx))["alerts"]["status"] == "warn"


async def test_repo_without_state_warns_indexed(ctx):
    got = (await _checks(ctx))["indexed"]
    assert got["status"] == "warn" and "kb-all" in got["detail"]
