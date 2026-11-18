"""search: the similarity floor (KH_SEARCH_MIN_SIMILARITY) and the per-hit similarity."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.errors import NORMATIVE
from knowledge_hub.mcp.server import Deps
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all
from tests.support.seed import add_chunked_doc, add_chunked_record

pytestmark = pytest.mark.integration

FLOOR = 0.4


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


async def hits(mcp_client, caller, **args):
    res = await mcp_client.call(caller, "search", args)
    assert not res.is_error, res.text
    return res.data["hits"]


async def test_floor_drops_an_unrelated_vector_neighbour_and_keeps_the_near_one(mcp_client, db, app_settings):
    near = add_chunked_doc(db, "marsupial words", vector_of="quokka")
    far = add_chunked_doc(db, "lorry timetable", vector_of="harbour cranes")
    db.commit()
    out = await hits(mcp_client, alice_all, query="quokka")
    assert {h["id"] for h in out} == {f"doc:{near}", f"doc:{far}"}
    app_settings.search_min_similarity = FLOOR
    out = await hits(mcp_client, alice_all, query="quokka")
    assert [h["id"] for h in out] == [f"doc:{near}"]


async def test_nothing_matches_returns_no_hits(mcp_client, db, app_settings):
    add_chunked_doc(db, "lorry timetable")
    add_chunked_record(db, "harbour cranes")
    db.commit()
    app_settings.search_min_similarity = FLOOR
    for args in ({}, {"min_tier": "T0"}):
        res = await mcp_client.call(alice_all, "search", {"query": "quokka", **args})
        assert not res.is_error, res.text
        assert res.data["hits"] == []
        assert res.data["next_cursor"] == ""


async def test_full_text_only_hit_survives_the_floor(mcp_client, db, app_settings):
    doc = add_chunked_doc(db, "quokka grass", vector_of="harbour cranes")
    db.commit()
    app_settings.search_min_similarity = FLOOR
    out = await hits(mcp_client, alice_all, query="quokka")
    assert [h["id"] for h in out] == [f"doc:{doc}"]
    assert out[0]["similarity"] < FLOOR


@pytest.mark.parametrize("tier", ["T0", "T0a"])
async def test_floor_lets_the_normative_rule_fire(tier, mcp_client, db, app_settings):
    family = add_chunked_doc(db, "family tree")
    add_chunked_record(db, "platypus ledger")
    db.commit()
    args = {"query": "platypus ledger", "min_tier": tier}
    out = await hits(mcp_client, alice_all, **args)
    assert [h["id"] for h in out] == [f"doc:{family}"]
    app_settings.search_min_similarity = FLOOR
    res = await mcp_client.call(alice_all, "search", args)
    assert res.is_error and res.text.endswith(NORMATIVE)


async def test_hits_carry_the_vector_similarity(mcp_client, db):
    same = add_chunked_doc(db, "numbat")
    textonly = add_chunked_doc(db, "numbat burrow", embed=False)
    db.commit()
    got = {h["id"]: h for h in await hits(mcp_client, alice_all, query="numbat")}
    assert got[f"doc:{same}"]["similarity"] == pytest.approx(1.0, abs=1e-3)
    assert got[f"doc:{textonly}"]["similarity"] is None


async def test_floor_keeps_paging_consistent(mcp_client, db, app_settings):
    near = {add_chunked_doc(db, f"echidna {n}", vector_of="echidna") for n in range(3)}
    lorry = add_chunked_doc(db, "lorry timetable")
    db.commit()
    app_settings.search_min_similarity = FLOOR
    first = await mcp_client.call(alice_all, "search", {"query": "echidna", "limit": 2})
    assert not first.is_error, first.text
    assert len(first.data["hits"]) == 2 and first.data["next_cursor"]
    second = await mcp_client.call(
        alice_all, "search", {"query": "echidna", "limit": 2, "cursor": first.data["next_cursor"]})
    assert not second.is_error, second.text
    assert len(second.data["hits"]) == 1 and second.data["next_cursor"] == ""
    ids = {h["id"] for h in first.data["hits"] + second.data["hits"]}
    assert ids == {f"doc:{d}" for d in near} and f"doc:{lorry}" not in ids
