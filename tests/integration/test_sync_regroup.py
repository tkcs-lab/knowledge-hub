"""Integration tests: a sync re-reads stored documents whose group differs from the config (plan 3.1)."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.config import GroupConfig, KhConfig, TechRepoConfig
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.forgejo import ForgejoReader
from knowledge_hub.indexer.sync import sync_repo
from tests.support.forgejo_fake import FakeForgejo, md

DB = "kh_fresh"
PEOPLE = {"alice": "alice@example.test", "bob": "bob@example.test"}


def cfg(all_repo: str, mgmt_repo: str) -> KhConfig:
    return KhConfig(
        people=PEOPLE,
        groups={
            "all": GroupConfig(repo=all_repo, members=["alice"], approvers=["alice"]),
            "mgmt": GroupConfig(repo=mgmt_repo, members=["bob"], approvers=["bob"]),
        },
    )


BEFORE = cfg("kb-shared", "kb-mgmt")
AFTER = cfg("kb-all", "kb-shared")


class NoMgmtEmbedder(FakeEmbedder):
    def may_embed(self, audience: str) -> bool:
        return audience != "mgmt"


@pytest.fixture
def world(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all'), ('mgmt', 'kb-mgmt')")
        conn.execute("INSERT INTO kh.labels (name) VALUES ('ember')")
    return fresh_db


@pytest.fixture
def fake():
    f = FakeForgejo()
    for repo in ("kb-all", "kb-mgmt", "kb-shared"):
        f.add_repo(repo)
    return f


@pytest.fixture
async def reader(fake):
    r = ForgejoReader("https://forgejo.test", "tok", "kh", transport=fake.transport)
    yield r
    await r.aclose()


def q(world, sql, *args):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        cur = conn.execute(sql, args)
        return cur.fetchall() if cur.description else []


async def sync_with(pool, reader, config, repo, embedder=None):
    return await sync_repo(pool, reader, config, repo, embedder or FakeEmbedder(1024), chunker_version=1)


async def test_regroup_moves_document_and_chunks_in_place(world, fake, reader, indexer_pool):
    fake.commit("kb-shared", {"a.md": md("a")})
    await sync_with(indexer_pool, reader, BEFORE, "kb-shared")
    before = q(world, "SELECT id, blob_sha, audience FROM kh.documents")
    assert before[0][2] == "all"
    await sync_with(indexer_pool, reader, AFTER, "kb-shared")
    after = q(world, "SELECT id, blob_sha, audience FROM kh.documents")
    assert after == [(before[0][0], before[0][1], "mgmt")]
    assert q(world, "SELECT DISTINCT audience FROM kh.chunks") == [("mgmt",)]


async def test_regroup_into_a_group_that_may_not_embed_drops_vectors(world, fake, reader, indexer_pool):
    fake.commit("kb-shared", {"a.md": md("a")})
    await sync_with(indexer_pool, reader, BEFORE, "kb-shared", NoMgmtEmbedder(1024))
    assert q(world, "SELECT bool_and(embedding IS NOT NULL) FROM kh.chunks") == [(True,)]
    await sync_with(indexer_pool, reader, AFTER, "kb-shared", NoMgmtEmbedder(1024))
    assert q(world, "SELECT audience, bool_and(embedding IS NULL) FROM kh.chunks GROUP BY 1") == [
        ("mgmt", True)
    ]


async def test_regrouped_file_is_validated_again(world, fake, reader, indexer_pool):
    fake.commit("kb-shared", {"a.md": md("a"), "e.md": md("e", labels=["ember"])})
    await sync_with(indexer_pool, reader, BEFORE, "kb-shared")
    q(world, "UPDATE kh.documents SET labels = '{}'")
    q(world, "DELETE FROM kh.labels WHERE name = 'ember'")
    await sync_with(indexer_pool, reader, AFTER, "kb-shared")
    assert q(world, "SELECT path, audience FROM kh.documents") == [("a.md", "mgmt")]
    assert q(world, "SELECT path, message FROM kh.index_alerts") == [
        ("e.md", "file rejected: unknown label ember")
    ]


async def test_row_of_another_tier_is_not_reread_and_vanishes(world, fake, indexer_pool):
    tech_fake = FakeForgejo(org="kh-tech")
    tech_fake.add_repo("ember-core")
    tech_fake.commit("ember-core", {"docs/b.md": "# B\n"})
    config = BEFORE.model_copy(
        update={"tech_org": "kh-tech", "tech_repos": {"ember-core": TechRepoConfig(dirs=["docs"])}}
    )
    q(
        world,
        "INSERT INTO kh.documents (repo, path, doc_key, audience, title, body, labels, review_by, stale,"
        " commit_sha, blob_sha, supersedes, tier)"
        " VALUES ('ember-core', 'docs/a.md', 'docs/a.md', 'mgmt', 'A', 'x', '{}', '2099-01-01', false,"
        " 'c', 'old', '{}', 'T0')",
    )
    r = ForgejoReader("https://forgejo.test", "tok", "kh-tech", transport=tech_fake.transport)
    try:
        await sync_repo(
            indexer_pool, None, config, "ember-core", FakeEmbedder(1024), chunker_version=1, tech_reader=r
        )
    finally:
        await r.aclose()
    assert q(world, "SELECT path, tier FROM kh.documents") == [("docs/b.md", "T0a")]
