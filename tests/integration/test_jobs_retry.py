"""``retry_jobs`` and ``reclaim_stuck``."""

from __future__ import annotations

import psycopg

from knowledge_hub.indexer.worker import MAX_ATTEMPTS
from knowledge_hub.jobs.retry import reclaim_stuck, retry_jobs

DB = "kh_fresh"


def conn_for(world):
    return psycopg.connect(world.admin_dsn(DB), autocommit=True)


def add_job(world, state, attempts=0, age="0 minutes", error=None):
    with conn_for(world) as conn:
        return conn.execute(
            "INSERT INTO kh.index_jobs (kind, payload, state, attempts, last_error, created_at)"
            " VALUES ('git_push', '{}'::jsonb, %s, %s, %s, now() - %s::interval) RETURNING id",
            (state, attempts, error, age),
        ).fetchone()[0]


def row(world, job_id):
    with conn_for(world) as conn:
        return conn.execute(
            "SELECT state, attempts, last_error FROM kh.index_jobs WHERE id = %s", (job_id,)
        ).fetchone()


async def test_dead_job_is_requeued_and_keeps_its_error(fresh_db, indexer_pool):
    dead = add_job(fresh_db, "failed", attempts=5, error="boom")
    assert await retry_jobs(indexer_pool) == 1
    assert row(fresh_db, dead) == ("queued", 0, "boom")


async def test_job_with_attempts_left_is_not_touched(fresh_db, indexer_pool):
    young = add_job(fresh_db, "failed", attempts=2, error="x")
    assert await retry_jobs(indexer_pool) == 0
    assert row(fresh_db, young) == ("failed", 2, "x")


async def test_ids_limit_the_update(fresh_db, indexer_pool):
    a = add_job(fresh_db, "failed", attempts=5)
    b = add_job(fresh_db, "failed", attempts=5)
    assert await retry_jobs(indexer_pool, [a]) == 1
    assert row(fresh_db, a)[0] == "queued"
    assert row(fresh_db, b)[0] == "failed"
    assert await retry_jobs(indexer_pool, []) == 0


async def test_stuck_running_job_is_reclaimed(fresh_db, indexer_pool):
    old = add_job(fresh_db, "running", age="2 hours")
    fresh = add_job(fresh_db, "running", age="1 minute")
    assert await reclaim_stuck(indexer_pool) == 1
    assert row(fresh_db, old)[0] == "queued"
    assert row(fresh_db, fresh)[0] == "running"


async def test_reclaimed_job_keeps_a_claimable_attempt_count(fresh_db, indexer_pool):
    # A job reclaimed during its last attempt must stay claimable (attempts < MAX_ATTEMPTS).
    last = add_job(fresh_db, "running", attempts=MAX_ATTEMPTS, age="2 hours")
    some = add_job(fresh_db, "running", attempts=2, age="2 hours")
    assert await reclaim_stuck(indexer_pool) == 2
    assert row(fresh_db, last)[:2] == ("queued", MAX_ATTEMPTS - 1)
    assert row(fresh_db, some)[:2] == ("queued", 2)
