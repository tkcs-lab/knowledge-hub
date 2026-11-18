"""ingest_record and propose_doc list similar items of the same group (plan 3.6)."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.forgejo import ForgejoBot
from knowledge_hub.mcp.server import Deps
from tests.support.blobs import MemoryBlobStore
from tests.support.forgejo_fake import FakeForgejo, blob_sha, md
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, multi
from tests.support.seed import add_chunked_doc, add_chunked_record

pytestmark = pytest.mark.integration

TITLE, BODY = "Quokka plan", "We agreed on the quokka plan."
ARGS = {
    "source": "meet", "source_ref": "m-1", "title": TITLE, "body": BODY, "audience": "all",
    "occurred_at": "2026-03-04T10:00:00+01:00",
}
QUERY = f"{TITLE}\n\n{BODY}"


class NoEmbed(FakeEmbedder):
    def may_embed(self, audience: str) -> bool:
        return False


class SlowEmbed(FakeEmbedder):
    async def embed(self, texts):
        await asyncio.sleep(1)
        return await super().embed(texts)


@pytest.fixture
def forgejo() -> FakeForgejo:
    fake = FakeForgejo()
    fake.add_repo("kb-all")
    fake.add_repo("kb-hr")
    return fake


@pytest.fixture
def deps(app_settings, kh_config, app_pool, indexer_pool, forgejo) -> Deps:
    return Deps(
        settings=app_settings, config=kh_config, app_pool=app_pool, indexer_pool=indexer_pool,
        embedder=FakeEmbedder(), blobs=MemoryBlobStore(),
        forgejo_bot=ForgejoBot("https://forgejo.test", "tok", "kh", transport=forgejo.transport),
    )


@pytest.fixture
def db(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        yield conn
        conn.commit()


def seed_rec(db, title="Old", body="old body", **fields):
    return add_chunked_record(db, body, vector_of=f"{title}\n\n{body}", title=title, **fields)


async def ingest(mcp_client, caller=alice_all, **args):
    res = await mcp_client.call(caller, "ingest_record", {**ARGS, **args})
    assert not res.is_error, res.text
    return res.data


async def test_identical_text_is_returned_with_similarity_one(mcp_client, db):
    rec = seed_rec(db, TITLE, BODY)
    db.commit()
    data = await ingest(mcp_client)
    assert data["similar"] == [{"id": f"rec:{rec}", "tier": "T3", "title": TITLE, "similarity": 1.0}]


async def test_unrelated_text_is_not_returned(mcp_client, db):
    seed_rec(db, "Other", "something else entirely")
    db.commit()
    assert (await ingest(mcp_client))["similar"] == []


async def test_other_group_items_are_never_returned(mcp_client, db):
    seed_rec(db, TITLE, BODY, audience="mgmt")
    db.commit()
    assert (await ingest(mcp_client, caller=multi))["similar"] == []


async def test_outdated_and_superseded_items_are_not_returned(mcp_client, db):
    seed_rec(db, TITLE, BODY, outdated=True, outdated_at="2026-01-02T00:00:00+00:00",
             outdated_by="alice", replaced_by="")
    new = add_chunked_doc(db, "unrelated successor words", embed=False)
    add_chunked_doc(db, BODY, vector_of=QUERY, title=TITLE, superseded_by=new)
    db.commit()
    assert (await ingest(mcp_client))["similar"] == []


async def test_one_row_per_owner_and_limit_applies(mcp_client, db, app_settings):
    app_settings.similar_limit = 1
    doc = add_chunked_doc(db, BODY, vector_of=QUERY, title=TITLE, chunks=3)
    seed_rec(db, TITLE, BODY)
    db.commit()
    data = await ingest(mcp_client)
    assert len(data["similar"]) == 1
    app_settings.similar_limit = 5
    ids = [s["id"] for s in (await ingest(mcp_client, source_ref="m-2"))["similar"]]
    assert ids.count(f"doc:{doc}") == 1 and len(ids) == len(set(ids))


async def test_limit_zero_switches_it_off(mcp_client, db, app_settings):
    seed_rec(db, TITLE, BODY)
    db.commit()
    app_settings.similar_limit = 0
    assert (await ingest(mcp_client))["similar"] == []


async def test_embedder_that_may_not_embed_the_audience_returns_empty(mcp_client, db, deps):
    seed_rec(db, TITLE, BODY)
    db.commit()
    deps.embedder = NoEmbed()
    assert (await ingest(mcp_client))["similar"] == []


async def test_failing_query_keeps_the_record(mcp_client, db, monkeypatch):
    monkeypatch.setattr("knowledge_hub.search.similar._SIMILAR_SQL", "SELECT nope FROM nowhere")
    data = await ingest(mcp_client)
    assert data["existed"] is False and data["similar"] == []
    (n,) = db.execute("SELECT count(*) FROM kh.records WHERE source_ref = 'm-1'").fetchone()
    assert n == 1


async def test_embedding_timeout_keeps_the_record(mcp_client, db, deps, monkeypatch):
    deps.embedder = SlowEmbed()
    monkeypatch.setattr("knowledge_hub.search.similar.EMBED_TIMEOUT_S", 0.05)
    data = await ingest(mcp_client)
    assert data["similar"] == []
    (n,) = db.execute("SELECT count(*) FROM kh.records WHERE source_ref = 'm-1'").fetchone()
    assert n == 1


async def test_existing_record_returns_no_similar_key(mcp_client, db):
    await ingest(mcp_client)
    again = await ingest(mcp_client)
    assert again["existed"] is True and "similar" not in again


async def test_propose_doc_excludes_the_document_being_edited(mcp_client, db, forgejo):
    title, body = "Guide", "# Heading\n\nSome text.\n"
    doc = add_chunked_doc(db, body, vector_of=f"{title}\n\n{body}", title=title, repo="kb-all",
                          doc_key="g-1", path="guides/g.md", blob_sha="b1")
    other = add_chunked_doc(db, body, vector_of=f"{title}\n\n{body}", title=title)
    db.commit()
    old = md("g-1", "Guide")
    forgejo.commit("kb-all", {"guides/g.md": old})
    args = {"group": "all", "path": "guides/g.md", "id": "g-1", "title": title,
            "review_by": "2027-06-30", "body": body, "base_blob": blob_sha(old)}
    res = await mcp_client.call(alice_all, "propose_doc", args)
    assert not res.is_error, res.text
    ids = [s["id"] for s in res.data["similar"]]
    assert f"doc:{other}" in ids and f"doc:{doc}" not in ids
