"""``queue_rebuild`` forgets the indexed head and queues one ``git_push`` job."""

from __future__ import annotations

import psycopg

from knowledge_hub.jobs.rebuild_queue import queue_rebuild

DB = "kh_fresh"


def q(world, sql, *args):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


async def test_queue_rebuild_deletes_state_and_queues_once(fresh_db, indexer_pool):
    q(fresh_db, "INSERT INTO kh.repo_state (repo, last_indexed_sha) VALUES ('kb-all', 'abc')")
    assert await queue_rebuild(indexer_pool, "kb-all") is True
    assert q(fresh_db, "SELECT count(*) FROM kh.repo_state WHERE repo = 'kb-all'") == [(0,)]
    assert q(fresh_db, "SELECT kind, repo, state FROM kh.index_jobs") == [("git_push", "kb-all", "queued")]
    assert await queue_rebuild(indexer_pool, "kb-all") is False
    assert q(fresh_db, "SELECT count(*) FROM kh.index_jobs") == [(1,)]
