"""Integration tests of chunk writing, reuse and embedding policy in the sync."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.forgejo import ForgejoReader
from knowledge_hub.indexer.sync import sync_repo
from tests.support.forgejo_fake import FakeForgejo, md

DB = "kh_fresh"
CFG = KhConfig(
    people={"alice": "alice@example.test", "bob": "bob@example.test"},
    groups={
        "all": GroupConfig(members=["alice"], approvers=["alice"]),
        "mgmt": GroupConfig(members=["bob"], approvers=["bob"]),
    },
)


class SelectiveEmbedder(FakeEmbedder):
    """Embeds only for the audiences it is given; counts the texts it embeds."""

    def __init__(self, allowed: set[str]) -> None:
        super().__init__(1024)
        self.allowed = allowed
        self.embedded = 0

    def may_embed(self, audience: str) -> bool:
        return audience in self.allowed

    async def embed(self, texts):
        self.embedded += len(texts)
        return await super().embed(texts)


def sectioned(alpha: str = "alpha", beta: str = "beta") -> str:
    return md("a", body=f"# One\n{alpha} text about gateways.\n\n# Two\n{beta} text about zebrafish.\n")


@pytest.fixture
def world(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        for name in CFG.groups:
            conn.execute(
                "INSERT INTO kh.groups (name, repo) VALUES (%s, %s)", (name, CFG.repo_for_group(name))
            )
    return fresh_db


@pytest.fixture
def fake():
    f = FakeForgejo()
    f.add_repo("kb-all")
    f.add_repo("kb-mgmt")
    return f


@pytest.fixture
async def reader(fake):
    r = ForgejoReader("https://forgejo.test", "tok", "kh", transport=fake.transport)
    yield r
    await r.aclose()


def q(world, sql, *args):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


async def sync(pool, reader, embedder, repo="kb-all", version=1):
    return await sync_repo(pool, reader, CFG, repo, embedder, chunker_version=version)


async def test_chunks_inherit_document_fields_and_are_embedded(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": sectioned()})
    await sync(indexer_pool, reader, FakeEmbedder(1024))
    got = q(
        world,
        "SELECT c.tier, c.audience, c.owner_key = 'doc:' || d.id, c.embedding IS NOT NULL, c.embedding_model,"
        " c.heading_path FROM kh.chunks c JOIN kh.documents d ON d.id = c.document_id ORDER BY c.ordinal",
    )
    assert got == [("T0", "all", True, True, "fake", "One"), ("T0", "all", True, True, "fake", "Two")]


async def test_second_sync_keeps_chunk_ids(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": sectioned()})
    emb = SelectiveEmbedder({"all"})
    await sync(indexer_pool, reader, emb)
    first = q(world, "SELECT id, ordinal FROM kh.chunks ORDER BY ordinal")
    fake.commit("kb-all", {"other.md": md("o")})  # a push that leaves a.md alone
    await sync(indexer_pool, reader, emb)
    assert (
        q(
            world,
            "SELECT id, ordinal FROM kh.chunks WHERE document_id = (SELECT id FROM kh.documents"
            " WHERE doc_key = 'a') ORDER BY ordinal",
        )
        == first
    )


async def test_edit_reuses_unchanged_chunks_and_embeds_only_the_new(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": sectioned()})
    emb = SelectiveEmbedder({"all"})
    await sync(indexer_pool, reader, emb)
    assert emb.embedded == 2
    first = q(world, "SELECT id, heading_path FROM kh.chunks ORDER BY ordinal")
    fake.commit("kb-all", {"a.md": sectioned(beta="gamma")})
    await sync(indexer_pool, reader, emb)
    assert emb.embedded == 3
    after = q(world, "SELECT id, heading_path, text FROM kh.chunks ORDER BY ordinal")
    assert after[0][:2] == first[0]
    assert after[1][0] != first[1][0] and "gamma" in after[1][2]


async def test_reordered_sections_keep_ids_and_update_ordinals(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": sectioned()})
    emb = SelectiveEmbedder({"all"})
    await sync(indexer_pool, reader, emb)
    ids = dict(q(world, "SELECT heading_path, id FROM kh.chunks"))
    swapped = md("a", body="# Two\nbeta text about zebrafish.\n\n# One\nalpha text about gateways.\n")
    fake.commit("kb-all", {"a.md": swapped})
    await sync(indexer_pool, reader, emb)
    assert emb.embedded == 2
    got = q(world, "SELECT heading_path, id, ordinal FROM kh.chunks ORDER BY ordinal")
    assert got == [("Two", ids["Two"], 0), ("One", ids["One"], 1)]


async def test_chunker_version_change_rewrites_chunks(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": sectioned()})
    emb = SelectiveEmbedder({"all"})
    await sync(indexer_pool, reader, emb, version=1)
    old = {r[0] for r in q(world, "SELECT id FROM kh.chunks")}
    fake.commit("kb-all", {"touch.md": md("t")})
    await sync(indexer_pool, reader, emb, version=2)  # a.md is unchanged: its chunks are not revisited
    assert old <= {r[0] for r in q(world, "SELECT id FROM kh.chunks")}
    fake.commit("kb-all", {"a.md": sectioned() + "\nmore\n"})
    await sync(indexer_pool, reader, emb, version=2)
    new = {
        r[0]
        for r in q(
            world,
            "SELECT c.id FROM kh.chunks c JOIN kh.documents d ON d.id = c.document_id WHERE d.doc_key = 'a'",
        )
    }
    assert len(new) == 2 and not (new & old)


async def test_embeds_only_when_the_embedder_allows_the_audience(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": sectioned()})
    fake.commit("kb-mgmt", {"m.md": md("m", body="# H\nsecret text.\n")})
    emb = SelectiveEmbedder({"all"})
    await sync(indexer_pool, reader, emb, "kb-all")
    await sync(indexer_pool, reader, emb, "kb-mgmt")
    got = q(
        world,
        "SELECT c.audience, c.embedding IS NOT NULL, c.embedding_model FROM kh.chunks c ORDER BY c.audience",
    )
    assert got == [("all", True, "fake"), ("all", True, "fake"), ("mgmt", False, None)]
    assert emb.embedded == 2


async def test_model_change_re_embeds(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": sectioned()})
    await sync(indexer_pool, reader, SelectiveEmbedder({"all"}))
    other = SelectiveEmbedder({"all"})
    other.model = "other-model"
    fake.commit("kb-all", {"a.md": sectioned() + "\n"})  # force a re-plan of a.md
    await sync(indexer_pool, reader, other)
    assert q(world, "SELECT DISTINCT embedding_model FROM kh.chunks") == [("other-model",)]
