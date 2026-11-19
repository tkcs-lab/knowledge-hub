"""``run_daily`` and ``requeue_embeddings``."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from knowledge_hub.jobs.daily import purge_identity, run_daily
from knowledge_hub.jobs.requeue import requeue_embeddings
from knowledge_hub.jobs.usage import purge_usage
from tests.support.seed import add_chunk, add_doc, add_record, add_usage

DB = "kh_fresh"


@pytest.fixture
def world(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all')")
    return fresh_db


def conn_for(world):
    return psycopg.connect(world.admin_dsn(DB), autocommit=True)


async def test_stale_flips_both_ways_and_reaches_chunks(world, indexer_pool):
    today = date.today()
    with conn_for(world) as conn:
        past = add_doc(conn, review_by=today - timedelta(days=1), stale=False)
        future = add_doc(conn, review_by=today + timedelta(days=1), stale=True)
        due = add_doc(conn, review_by=today, stale=False)
        ok = add_doc(conn, review_by=today + timedelta(days=30), stale=False)
        add_chunk(conn, document_id=past)
    assert await run_daily(indexer_pool) == 2
    with conn_for(world) as conn:
        got = dict(conn.execute("SELECT id::text, stale FROM kh.documents").fetchall())
        assert got == {past: True, future: False, due: False, ok: False}
        assert conn.execute("SELECT stale FROM kh.chunks").fetchall() == [(True,)]
    assert await run_daily(indexer_pool) == 0


async def test_requeue_inserts_a_job_per_record_without_chunks(world, indexer_pool):
    with conn_for(world) as conn:
        bare = add_record(conn)
        covered = add_record(conn)
        add_chunk(conn, record_id=covered, tier="T3")
        waiting = add_record(conn)
        conn.execute("DELETE FROM kh.index_jobs")  # the insert trigger queued one per record
        conn.execute(
            "INSERT INTO kh.index_jobs (kind, payload)"
            " VALUES ('embed_record', jsonb_build_object('id', %s::text))",
            (waiting,),
        )
    assert await requeue_embeddings(indexer_pool) == 1
    with conn_for(world) as conn:
        rows = conn.execute("SELECT kind, state, payload ->> 'id' FROM kh.index_jobs ORDER BY id").fetchall()
    assert rows == [("embed_record", "queued", waiting), ("embed_record", "queued", bare)]
    assert await requeue_embeddings(indexer_pool) == 0


async def test_requeue_ignores_a_job_stuck_in_running(world, indexer_pool):
    with conn_for(world) as conn:
        stuck = add_record(conn)
        conn.execute("DELETE FROM kh.index_jobs")
        conn.execute(
            "INSERT INTO kh.index_jobs (kind, state, payload)"
            " VALUES ('embed_record', 'running', jsonb_build_object('id', %s::text))",
            (stuck,),
        )
    assert await requeue_embeddings(indexer_pool) == 1
    with conn_for(world) as conn:
        rows = conn.execute(
            "SELECT state FROM kh.index_jobs WHERE payload ->> 'id' = %s ORDER BY id", (stuck,)
        ).fetchall()
    assert ("queued",) in rows


async def test_purge_usage_deletes_only_events_older_than_the_window(world, indexer_pool):
    now = datetime.now(UTC)
    with conn_for(world) as conn:
        add_usage(conn, at=now - timedelta(days=200))
        keep = add_usage(conn, at=now - timedelta(days=1))
    assert await purge_usage(indexer_pool, 180) == 1
    with conn_for(world) as conn:
        assert conn.execute("SELECT id::text FROM kh.usage_events").fetchall() == [(keep,)]
    assert await purge_usage(indexer_pool, 180) == 0


async def test_daily_prints_the_usage_purge(world, deps, capsys):
    from knowledge_hub.cli import _daily

    with conn_for(world) as conn:
        add_usage(conn, at=datetime.now(UTC) - timedelta(days=200))
    assert await _daily(deps) == 0
    assert capsys.readouterr().out.strip().endswith("usage events purged: 1")


async def test_purge_identity_removes_expired_oauth_state(world, indexer_pool):
    with conn_for(world) as conn:
        conn.execute(
            "INSERT INTO kh.oauth_codes (code_sha256, client_id, email, params, expires_at)"
            " VALUES ('\\x01', 'c', 'a@x.org', '{}', now() - interval '1 minute'),"
            "        ('\\x02', 'c', 'a@x.org', '{}', now() + interval '1 minute')"
        )
    assert await purge_identity(indexer_pool) == 1
    with conn_for(world) as conn:
        assert conn.execute("SELECT count(*) FROM kh.oauth_codes").fetchone() == (1,)
    assert await purge_identity(indexer_pool) == 0
