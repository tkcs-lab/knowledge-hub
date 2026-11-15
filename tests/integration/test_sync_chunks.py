"""Integration tests of chunk writing, embedding and idempotence in the sync (plan 3.6, 3.10)."""

from __future__ import annotations

import httpx
import psycopg

from knowledge_hub.config import parse_config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.embed.http import HttpEmbedder
from knowledge_hub.indexer.sync import sync_repo
from tests.support.forgejo_fake import ForgejoFake
from tests.unit.test_sync_plan import merged, repo_of

DB = "kh_fresh"
CFG = parse_config({"repos": {"canonical-all": {"tier": "T0", "audience": "all", "trust_anchor": "a0"}}})
DIM = 1024


def rows(fresh_db, sql, *args):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


def sectioned(id_: str, alpha: str = "alpha", beta: str = "beta") -> str:
    return (
        f"---\nid: {id_}\ntitle: T\ndomain: product\nreview_by: 2027-01-01\n---\n"
        f"# One\n{alpha} text about gateways.\n\n# Two\n{beta} text about zebrafish.\n"
    )


async def sync(pool, repo_obj, embedder):
    return await sync_repo(pool, ForgejoFake({"canonical-all": repo_obj}), CFG, "canonical-all", embedder)


def one_doc(text: str):
    return repo_of([merged("m1", "a0", {"a.md": text}, 1)])


async def test_chunks_inherit_document_fields(fresh_db, indexer_pool):
    await sync(indexer_pool, one_doc(sectioned("a")), FakeEmbedder(DIM))
    got = rows(
        fresh_db,
        "SELECT c.tier, c.status, c.required_audiences, c.allowed_emails, c.domain,"
        " c.owner_key = 'doc:' || d.id, c.embedding IS NOT NULL, c.embedding_model"
        " FROM kh.chunks c JOIN kh.documents d ON d.id = c.document_id",
    )
    assert len(got) == 2
    assert all(r == ("T0", "current", ["all"], None, "product", True, True, "fake") for r in got)


async def test_second_sync_keeps_chunk_ids_and_indexed_at(fresh_db, indexer_pool):
    r = one_doc(sectioned("a"))
    await sync(indexer_pool, r, FakeEmbedder(DIM))
    q = (
        "SELECT c.id, c.ordinal, d.indexed_at FROM kh.chunks c JOIN kh.documents d ON d.id = c.document_id"
        " ORDER BY c.ordinal"
    )
    first = rows(fresh_db, q)
    await sync(indexer_pool, r, FakeEmbedder(DIM))
    assert rows(fresh_db, q) == first and len(first) == 2


async def test_changing_one_section_rewrites_only_its_chunk(fresh_db, indexer_pool):
    await sync(indexer_pool, one_doc(sectioned("a")), FakeEmbedder(DIM))
    before = dict(rows(fresh_db, "SELECT heading_path, id FROM kh.chunks"))
    await sync(indexer_pool, one_doc(sectioned("a", beta="gamma")), FakeEmbedder(DIM))
    after = dict(rows(fresh_db, "SELECT heading_path, id FROM kh.chunks"))
    assert after["One"] == before["One"]
    assert after["Two"] != before["Two"]
    assert rows(fresh_db, "SELECT count(*) FROM kh.chunks") == [(2,)]


async def test_removed_document_removes_chunks(fresh_db, indexer_pool):
    await sync(indexer_pool, one_doc(sectioned("a")), FakeEmbedder(DIM))
    assert rows(fresh_db, "SELECT count(*) FROM kh.chunks") == [(2,)]
    empty = repo_of([merged("m1", "a0", {"b.md": sectioned("b")}, 1)])
    await sync(indexer_pool, empty, FakeEmbedder(DIM))
    assert rows(
        fresh_db,
        "SELECT count(*) FROM kh.chunks c JOIN kh.documents d ON d.id = c.document_id WHERE d.doc_key = 'a'",
    ) == [(0,)]


class OtherModel(FakeEmbedder):
    model = "fake-2"


async def test_changing_embedding_model_reembeds_all_chunks(fresh_db, indexer_pool):
    r = one_doc(sectioned("a"))
    await sync(indexer_pool, r, FakeEmbedder(DIM))
    before = {i for (i,) in rows(fresh_db, "SELECT id FROM kh.chunks")}
    await sync(indexer_pool, r, OtherModel(DIM))
    assert rows(fresh_db, "SELECT DISTINCT embedding_model FROM kh.chunks") == [("fake-2",)]
    assert not before & {i for (i,) in rows(fresh_db, "SELECT id FROM kh.chunks")}
    assert rows(fresh_db, "SELECT count(*) FROM kh.chunks") == [(2,)]


def http_embedder(allowed: set[str]) -> HttpEmbedder:
    fake = FakeEmbedder(DIM)

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        texts = json.loads(request.content)["input"]
        return httpx.Response(200, json={"data": [{"embedding": v} for v in await fake.embed(texts)]})

    return HttpEmbedder("http://emb/v1", "tok", "m", DIM, allowed, transport=httpx.MockTransport(handler))


async def test_refused_embedding_is_null_and_full_text_finds_it(fresh_db, indexer_pool):
    await sync(indexer_pool, one_doc(sectioned("a")), http_embedder({"eng"}))
    assert rows(fresh_db, "SELECT count(*), count(embedding), max(embedding_model) FROM kh.chunks") == [
        (2, 0, None)
    ]
    hit = rows(
        fresh_db,
        "SELECT heading_path FROM kh.chunks WHERE tsv @@ websearch_to_tsquery('english', 'zebrafish')",
    )
    assert hit == [("Two",)]
    # Widening the allowed audiences re-embeds the same chunks.
    await sync(indexer_pool, one_doc(sectioned("a")), http_embedder({"all"}))
    assert rows(fresh_db, "SELECT count(*), count(embedding) FROM kh.chunks") == [(2, 2)]
