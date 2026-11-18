"""search with tier T0a: ranking, min_tier and the two normative errors."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.errors import NORMATIVE, NORMATIVE_T0A
from knowledge_hub.mcp.server import Deps
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all
from tests.support.seed import add_chunked_doc, add_chunked_record

pytestmark = pytest.mark.integration


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
def deps(app_settings, kh_config, app_pool, indexer_pool) -> Deps:
    return Deps(
        settings=app_settings, config=kh_config, app_pool=app_pool, indexer_pool=indexer_pool,
        embedder=FakeEmbedder(),
    )


@pytest.fixture
def db(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        yield conn
        conn.commit()


async def hits(mcp_client, **args):
    res = await mcp_client.call(alice_all, "search", args)
    assert not res.is_error, res.text
    return res.data["hits"]


async def test_default_search_ranks_t0_above_t0a_above_t3(mcp_client, db):
    t0 = add_chunked_doc(db, "numbat habitat")
    t0a = add_chunked_doc(db, "numbat habitat", tier="T0a")
    rec = add_chunked_record(db, "numbat habitat")
    db.commit()
    out = await hits(mcp_client, query="numbat habitat")
    assert [h["id"] for h in out] == [f"doc:{t0}", f"doc:{t0a}", f"rec:{rec}"]
    assert [h["tier"] for h in out] == ["T0", "T0a", "T3"]
    assert out[0]["score"] > out[1]["score"] > out[2]["score"]


async def test_min_tier_t0a_returns_t0_and_t0a_but_not_t3(mcp_client, db):
    t0 = add_chunked_doc(db, "bilby burrow")
    t0a = add_chunked_doc(db, "bilby burrow", tier="T0a")
    add_chunked_record(db, "bilby burrow")
    db.commit()
    out = await hits(mcp_client, query="bilby burrow", min_tier="T0a")
    assert {h["id"] for h in out} == {f"doc:{t0}", f"doc:{t0a}"}


async def test_min_tier_t0_fails_with_t0a_text_when_only_t0a_matches(mcp_client, db):
    add_chunked_doc(db, "dingo pack", tier="T0a")
    add_chunked_record(db, "dingo pack")
    db.commit()
    res = await mcp_client.call(alice_all, "search", {"query": "dingo pack", "min_tier": "T0"})
    assert res.is_error and res.text.endswith(NORMATIVE_T0A)


async def test_min_tier_t0_fails_with_t3_text_when_only_t3_matches(mcp_client, db):
    add_chunked_record(db, "echidna spines")
    db.commit()
    res = await mcp_client.call(alice_all, "search", {"query": "echidna spines", "min_tier": "T0"})
    assert res.is_error and res.text.endswith(NORMATIVE)


async def test_min_tier_t0a_fails_when_only_t3_matches(mcp_client, db):
    add_chunked_record(db, "wallaby hop")
    db.commit()
    res = await mcp_client.call(alice_all, "search", {"query": "wallaby hop", "min_tier": "T0a"})
    assert res.is_error and res.text.endswith(NORMATIVE)


async def test_t0a_hit_has_tier_repo_and_path(mcp_client, db):
    doc = add_chunked_doc(db, "kookaburra laugh", tier="T0a", repo="kb-all", path="docs/k.md")
    db.commit()
    (hit,) = await hits(mcp_client, query="kookaburra laugh")
    assert hit["id"] == f"doc:{doc}"
    assert (hit["tier"], hit["repo"], hit["path"]) == ("T0a", "kb-all", "docs/k.md")
