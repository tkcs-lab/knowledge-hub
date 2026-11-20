"""search: the query is embedded only when every group it covers may be embedded."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.server import Deps
from tests.support.bearer import TestBearerResolver
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, dave_none, multi
from tests.support.seed import add_chunked_doc, add_chunked_record

pytestmark = pytest.mark.integration


class StubEmbedder:
    model = "fake"
    dim = 1024

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._inner = FakeEmbedder(self.dim)

    def may_embed(self, audience: str) -> bool:
        return audience == "all"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return await self._inner.embed(texts)


@pytest.fixture
def cursor_key(tmp_path):
    path = tmp_path / "cursor.key"
    path.write_bytes(b"k" * 32)
    return path


@pytest.fixture
def app_settings(db_settings, cursor_key):
    from tests.support.mcp_client import HOST

    update = {"env": "test", "allowed_hosts": HOST, "cursor_key_file": cursor_key}
    return db_settings.model_copy(update=update)


@pytest.fixture
def stub() -> StubEmbedder:
    return StubEmbedder()


@pytest.fixture
def deps(app_settings, kh_config, app_pool, indexer_pool, stub) -> Deps:
    return Deps(
        settings=app_settings, config=kh_config, app_pool=app_pool, indexer_pool=indexer_pool,
        embedder=stub, bearer=TestBearerResolver(kh_config),
    )


@pytest.fixture
def db(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        yield conn
        conn.commit()


async def hits(mcp_client, caller, **args):
    res = await mcp_client.call(caller, "search", args)
    assert not res.is_error, res.text
    return res.data["hits"]


def _seed(db):
    doc = add_chunked_doc(db, "quokka notes")
    rec = add_chunked_record(db, "quokka minutes")
    other = add_chunked_doc(db, "lorry timetable")
    db.commit()
    return doc, rec, other


async def test_multi_group_caller_without_audience_is_full_text_only(mcp_client, db, stub):
    doc, rec, other = _seed(db)
    out = await hits(mcp_client, multi, query="quokka")
    assert stub.calls == []
    assert {h["id"] for h in out} == {f"doc:{doc}", f"rec:{rec}"}
    assert all(h["similarity"] is None for h in out)


async def test_audience_all_embeds_the_query(mcp_client, db, stub):
    _seed(db)
    out = await hits(mcp_client, multi, query="quokka", audience="all")
    assert len(stub.calls) == 1
    assert out and all(h["similarity"] is not None for h in out)


async def test_single_embeddable_group_embeds(mcp_client, db, stub):
    _seed(db)
    await hits(mcp_client, alice_all, query="quokka")
    assert len(stub.calls) == 1


async def test_caller_without_groups_never_embeds(mcp_client, db, stub):
    _seed(db)
    assert await hits(mcp_client, dave_none, query="quokka") == []
    assert stub.calls == []


async def test_audience_not_held_never_embeds(mcp_client, db, stub):
    _seed(db)
    await hits(mcp_client, multi, query="quokka", audience="hr")
    assert stub.calls == []
