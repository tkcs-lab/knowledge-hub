"""The hourly poll queues one ``git_push`` job per tech repo."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.worker import Worker
from tests.support.tech import TECH_CONFIG, TECH_REPO

DB = "kh_fresh"


@pytest.fixture
def world(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        for name in TECH_CONFIG.groups:
            conn.execute(
                "INSERT INTO kh.groups (name, repo) VALUES (%s, %s)",
                (name, TECH_CONFIG.repo_for_group(name)),
            )
    return fresh_db


def q(world, sql, *args):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        cur = conn.execute(sql, args)
        return cur.fetchall() if cur.description else []


def jobs(world):
    return q(world, "SELECT kind, repo, state FROM kh.index_jobs ORDER BY id")


@pytest.fixture
def make_worker(indexer_pool):
    def make(**kwargs):
        args = dict(chunker_version=1, poll_seconds=0.02, tech_poll_seconds=3600.0)
        args.update(kwargs)
        return Worker(indexer_pool, None, FakeEmbedder(1024), TECH_CONFIG, **args)

    return make


async def test_poll_enqueues_one_job_per_tech_repo(world, make_worker):
    await make_worker().poll_tech_repos()
    assert jobs(world) == [("git_push", TECH_REPO, "queued")]


async def test_poll_skips_a_repo_with_a_queued_job(world, make_worker):
    q(world, "INSERT INTO kh.index_jobs (kind, repo) VALUES ('git_push', %s)", TECH_REPO)
    await make_worker().poll_tech_repos()
    assert jobs(world) == [("git_push", TECH_REPO, "queued")]


async def test_poll_skips_a_repo_with_a_recent_running_job(world, make_worker):
    q(world, "INSERT INTO kh.index_jobs (kind, repo, state) VALUES ('git_push', %s, 'running')", TECH_REPO)
    await make_worker().poll_tech_repos()
    assert jobs(world) == [("git_push", TECH_REPO, "running")]


async def test_poll_requeues_a_repo_whose_job_was_left_running(world, make_worker):
    q(
        world,
        "INSERT INTO kh.index_jobs (kind, repo, state, created_at)"
        " VALUES ('git_push', %s, 'running', now() - interval '2 hours')",
        TECH_REPO,
    )
    await make_worker().poll_tech_repos()
    assert jobs(world) == [("git_push", TECH_REPO, "running"), ("git_push", TECH_REPO, "queued")]


async def test_poll_disabled_with_zero_seconds(world, make_worker):
    worker = make_worker(tech_poll_seconds=0.0)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.2)
    worker.stop()
    await asyncio.wait_for(task, 5)
    assert jobs(world) == []


async def test_run_forever_polls_before_the_first_claim(world, make_worker):
    claimed_with = []
    worker = make_worker()
    original = worker.run_once

    async def spy():
        claimed_with.append(len(jobs(world)))
        return await original()

    worker.run_once = spy
    task = asyncio.create_task(worker.run_forever())
    for _ in range(100):
        if claimed_with:
            break
        await asyncio.sleep(0.02)
    worker.stop()
    await asyncio.wait_for(task, 5)
    assert claimed_with[0] == 1  # the poll's job existed before the first claim
    assert len(jobs(world)) == 1  # one hour has not passed, so no second poll


async def test_a_poll_error_does_not_stop_the_loop(world, make_worker, caplog):
    worker = make_worker()
    calls = []

    async def boom():
        calls.append(1)
        raise RuntimeError("poll failed")

    worker.poll_tech_repos = boom
    q(world, "INSERT INTO kh.index_jobs (kind, repo) VALUES ('git_push', 'kb-all')")
    task = asyncio.create_task(worker.run_forever())
    for _ in range(100):
        if q(world, "SELECT state FROM kh.index_jobs") != [("queued",)]:
            break
        await asyncio.sleep(0.02)
    worker.stop()
    await asyncio.wait_for(task, 5)
    assert calls == [1]
    assert "worker loop error" in caplog.text
    assert q(world, "SELECT state FROM kh.index_jobs") != [("queued",)]  # the loop still claimed the job
