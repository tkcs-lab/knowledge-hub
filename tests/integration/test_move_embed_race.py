"""A move that commits while the worker embeds must not leave chunks with the old audience."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.worker import Worker
from tests.support.blobs import MemoryBlobStore
from tests.support.principals import KH_CONFIG

DB = "kh_fresh"


@pytest.fixture
def world(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        for name in KH_CONFIG.groups:
            conn.execute("INSERT INTO kh.groups (name, repo) VALUES (%s, %s)",
                         (name, KH_CONFIG.repo_for_group(name)))
    return fresh_db


def q(world, sql, *args):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


class MovingEmbedder(FakeEmbedder):
    """Commits an audience change of the record, then returns the vectors."""

    def __init__(self, world, rec: str) -> None:
        super().__init__(1024)
        self.world, self.rec = world, rec

    async def embed(self, texts):
        with psycopg.connect(self.world.admin_dsn(DB), autocommit=True) as conn:
            conn.execute("UPDATE kh.records SET audience = 'mgmt' WHERE id = %s", (self.rec,))
        return await super().embed(texts)


async def test_audience_change_during_embedding_fails_the_job(world, indexer_pool):
    blobs = MemoryBlobStore()
    await blobs.put("records/r1", b"# Notes\nalpha beta\n", "text/plain")
    [(rec,)] = q(
        world,
        "INSERT INTO kh.records (audience, source, source_ref, title, blob_key, excerpt, content_type,"
        " occurred_at, written_by, created_by_sub, created_by_type)"
        " VALUES ('all', 's', 'r', 'R', 'records/r1', 'x', 'text/plain', now(), 'kh-bot', 'sub',"
        " 'claude_client') RETURNING id::text")
    worker = Worker(indexer_pool, None, MovingEmbedder(world, rec), KH_CONFIG, blobs, chunker_version=1)
    assert await worker.run_once() is True
    [(state, err)] = q(world, "SELECT state, last_error FROM kh.index_jobs WHERE kind = 'embed_record'")
    assert state == "failed" and "record audience changed" in err
    assert q(world, "SELECT count(*) FROM kh.chunks WHERE record_id = %s AND audience = 'all'", rec) == [(0,)]
