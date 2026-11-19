"""In-process pipeline ops against a real database."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.indexer.worker import MAX_ATTEMPTS
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.ops import OpError
from knowledge_hub.panel.pipeline_ops import run_inproc
from knowledge_hub.panel.settings import PanelSettings
from tests.support.pg import FRESH_DB

pytestmark = pytest.mark.integration


def _config() -> KhConfig:
    return KhConfig(
        people={"alice": "alice@example.test"},
        groups={"all": GroupConfig(members=["alice"], approvers=["alice"], repo="kb-all")},
    )


@pytest.fixture
def ctx(fresh_db, indexer_pool, tmp_path):
    cfg_file = tmp_path / "kh.yaml"
    cfg_file.write_text("")
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        conn.execute("DELETE FROM kh.index_jobs")
    return PanelContext(
        settings=PanelSettings(env="dev", config_file=cfg_file, dev_admin="admin@example.test"),
        signer=None,
        upstream=None,
        load_config=_config,
        pool=indexer_pool,
    )


def _q(world, sql, params=()):
    with psycopg.connect(world.admin_dsn(FRESH_DB), autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def _job(world, state, attempts=0, age="0 minutes"):
    with psycopg.connect(world.admin_dsn(FRESH_DB), autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO kh.index_jobs (kind, payload, state, attempts, created_at)"
            " VALUES ('git_push', '{}'::jsonb, %s, %s, now() - %s::interval) RETURNING id",
            (state, attempts, age),
        ).fetchone()[0]


async def test_jobs_retry(ctx, fresh_db):
    dead = _job(fresh_db, "failed", attempts=MAX_ATTEMPTS)
    other = _job(fresh_db, "failed", attempts=MAX_ATTEMPTS)
    assert await run_inproc("jobs.retry", {"ids": [dead]}, ctx) == "retried 1 job"
    assert _q(fresh_db, "SELECT state FROM kh.index_jobs WHERE id = %s", (dead,)) == [("queued",)]
    assert _q(fresh_db, "SELECT state FROM kh.index_jobs WHERE id = %s", (other,)) == [("failed",)]
    assert await run_inproc("jobs.retry", {}, ctx) == "retried 1 job"


async def test_jobs_reclaim(ctx, fresh_db):
    old = _job(fresh_db, "running", age="2 hours")
    young = _job(fresh_db, "running", age="1 minute")
    assert await run_inproc("jobs.reclaim", {}, ctx) == "reclaimed 1 job"
    assert _q(fresh_db, "SELECT state FROM kh.index_jobs WHERE id = %s", (old,)) == [("queued",)]
    assert _q(fresh_db, "SELECT state FROM kh.index_jobs WHERE id = %s", (young,)) == [("running",)]


async def test_repo_rebuild(ctx, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.repo_state (repo, last_indexed_sha) VALUES ('kb-all', 'abc')")
    assert await run_inproc("repo.rebuild", {"repo": "kb-all"}, ctx) == "queued rebuild of kb-all"
    assert _q(fresh_db, "SELECT count(*) FROM kh.repo_state WHERE repo = 'kb-all'") == [(0,)]
    assert _q(fresh_db, "SELECT kind, repo, state FROM kh.index_jobs") == [("git_push", "kb-all", "queued")]
    assert await run_inproc("repo.rebuild", {"repo": "kb-all"}, ctx) == "rebuild of kb-all is already queued"


async def test_repo_rebuild_refuses_unconfigured_repo(ctx, fresh_db):
    with pytest.raises(OpError, match="repo kb-other is not configured"):
        await run_inproc("repo.rebuild", {"repo": "kb-other"}, ctx)
    assert _q(fresh_db, "SELECT count(*) FROM kh.index_jobs") == [(0,)]
