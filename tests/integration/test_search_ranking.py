"""search: tier weights, stale weight, RRF, labels, audience, cursor and the NORMATIVE rule."""

from __future__ import annotations

import json

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.errors import CURSOR_FOREIGN, NORMATIVE
from knowledge_hub.mcp.server import Deps
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, multi
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


async def hits(mcp_client, caller, **args):
    res = await mcp_client.call(caller, "search", args)
    assert not res.is_error, res.text
    return res.data["hits"]


async def test_hit_fields_and_snippet(mcp_client, db):
    doc = add_chunked_doc(db, "quokka " + "x" * 600, title="Quokka", labels=["ember"], blob_sha="bl1",
                          path="q.md", repo="kb-all")
    rec = add_chunked_record(db, "quokka record", title="QR", source_url="https://s/1")
    db.commit()
    got = {h["id"]: h for h in await hits(mcp_client, alice_all, query="quokka", min_tier="T3")}
    d, r = got[f"doc:{doc}"], got[f"rec:{rec}"]
    assert len(d["snippet"]) == 400 and d["snippet"].startswith("quokka")
    assert (d["tier"], d["title"], d["audience"], d["labels"], d["repo"], d["path"], d["blob_sha"]) == (
        "T0", "Quokka", "all", ["ember"], "kb-all", "q.md", "bl1")
    assert (d["source_url"], d["occurred_at"], d["stale"]) == ("", "", False)
    assert (r["tier"], r["title"], r["source_url"], r["repo"], r["path"], r["blob_sha"]) == (
        "T3", "QR", "https://s/1", "", "", "")
    assert r["occurred_at"].startswith("2026-01-02")


async def test_tier_weight_orders_t0_before_t3_and_follows_the_setting(mcp_client, db, app_settings):
    doc = add_chunked_doc(db, "wombat habitat")
    rec = add_chunked_record(db, "wombat habitat")
    db.commit()
    out = await hits(mcp_client, alice_all, query="wombat habitat", min_tier="T3")
    assert [h["id"] for h in out] == [f"doc:{doc}", f"rec:{rec}"]
    assert out[0]["score"] > out[1]["score"]
    # same fixtures, weights flipped
    app_settings.tier_weights = "T0:0.1,T3:1.0"
    out = await hits(mcp_client, alice_all, query="wombat habitat", min_tier="T3")
    assert [h["id"] for h in out] == [f"rec:{rec}", f"doc:{doc}"]


async def test_stale_weight_demotes_a_better_match(mcp_client, db):
    stale = add_chunked_doc(db, "alpha beta", stale=True)
    fresh = add_chunked_doc(db, "alpha beta gamma delta epsilon")
    db.commit()
    out = await hits(mcp_client, alice_all, query="alpha beta")
    assert [h["id"] for h in out] == [f"doc:{fresh}", f"doc:{stale}"]
    assert [h["stale"] for h in out] == [False, True]


async def test_rrf_rewards_a_chunk_found_by_both_lists(mcp_client, db):
    both = add_chunked_doc(db, "quokka")
    vec_only = add_chunked_doc(db, "marsupial unrelated words", vector_of="quokkas")
    fts_only = add_chunked_doc(db, "quokka grass", embed=False)
    db.commit()
    out = await hits(mcp_client, alice_all, query="quokka")
    ids = [h["id"] for h in out]
    assert ids[0] == f"doc:{both}"
    assert {f"doc:{vec_only}", f"doc:{fts_only}"} <= set(ids)


async def test_one_best_chunk_per_owner(mcp_client, db):
    doc = add_chunked_doc(db, "numbat", chunks=3)
    db.commit()
    out = await hits(mcp_client, alice_all, query="numbat")
    assert [h["id"] for h in out] == [f"doc:{doc}"]


async def test_labels_are_all_of(mcp_client, db):
    one = add_chunked_doc(db, "dingo", labels=["ember"])
    both = add_chunked_doc(db, "dingo", labels=["ember", "other"])
    none = add_chunked_doc(db, "dingo")
    db.commit()
    async def ids(**args):
        return {h["id"] for h in await hits(mcp_client, alice_all, query="dingo", **args)}

    assert await ids() == {f"doc:{one}", f"doc:{both}", f"doc:{none}"}
    assert await ids(labels=["ember"]) == {f"doc:{one}", f"doc:{both}"}
    assert await ids(labels=["ember", "other"]) == {f"doc:{both}"}
    assert await ids(labels=["nope"]) == set()


async def test_audience_filter(mcp_client, db):
    a = add_chunked_doc(db, "kookaburra", audience="all")
    m = add_chunked_doc(db, "kookaburra", audience="mgmt", repo="kb-mgmt")
    db.commit()
    both = {h["id"] for h in await hits(mcp_client, multi, query="kookaburra")}
    assert both == {f"doc:{a}", f"doc:{m}"}
    only = await hits(mcp_client, multi, query="kookaburra", audience="mgmt")
    assert [h["id"] for h in only] == [f"doc:{m}"]
    assert await hits(mcp_client, alice_all, query="kookaburra", audience="mgmt") == []


async def test_cursor_pages_and_is_bound_to_caller_and_query(mcp_client, db):
    docs = [add_chunked_doc(db, f"echidna {n}") for n in range(3)]
    db.commit()
    first = await mcp_client.call(alice_all, "search", {"query": "echidna", "limit": 1})
    assert not first.is_error and first.data["next_cursor"]
    cur = first.data["next_cursor"]
    second = await mcp_client.call(alice_all, "search", {"query": "echidna", "limit": 1, "cursor": cur})
    assert not second.is_error
    assert second.data["hits"][0]["id"] != first.data["hits"][0]["id"]
    assert {h["id"] for r in (first, second) for h in r.data["hits"]} <= {f"doc:{d}" for d in docs}
    other_caller = await mcp_client.call(bob_mgmt, "search", {"query": "echidna", "limit": 1, "cursor": cur})
    assert other_caller.is_error and other_caller.text.endswith(CURSOR_FOREIGN)
    other_query = await mcp_client.call(alice_all, "search", {"query": "wallaby", "limit": 1, "cursor": cur})
    assert other_query.is_error and other_query.text.endswith(CURSOR_FOREIGN)
    other_filter = await mcp_client.call(
        alice_all, "search", {"query": "echidna", "limit": 1, "cursor": cur, "labels": ["ember"]})
    assert other_filter.is_error and other_filter.text.endswith(CURSOR_FOREIGN)


async def test_normative_when_only_t3_matches(mcp_client, db):
    rec = add_chunked_record(db, "platypus ledger")
    db.commit()
    res = await mcp_client.call(alice_all, "search", {"query": "platypus", "min_tier": "T0"})
    assert res.is_error and res.text.endswith(NORMATIVE)
    out = await hits(mcp_client, alice_all, query="platypus", min_tier="T3")
    assert [h["id"] for h in out] == [f"rec:{rec}"]
    doc = add_chunked_doc(db, "platypus handbook")
    db.commit()
    out = await hits(mcp_client, alice_all, query="platypus", min_tier="T0")  # T0 hit exists: T3 stays out
    assert [h["id"] for h in out] == [f"doc:{doc}"]


async def test_default_searches_both_tiers(mcp_client, db):
    doc = add_chunked_doc(db, "capybara handbook")
    rec = add_chunked_record(db, "capybara ledger")
    db.commit()
    out = await hits(mcp_client, alice_all, query="capybara")
    assert [h["id"] for h in out] == [f"doc:{doc}", f"rec:{rec}"]


def test_tier_weights_setting_is_validated():
    from knowledge_hub.settings import Settings

    for bad in ("T0:1.0", "T0:1.0,T1:0.5,T3:0.4", "T0:1.0,T3:0", "T0:1.0,T3:-1", "T0:x,T3:1"):
        with pytest.raises(ValueError):
            Settings(tier_weights=bad)
    assert json.dumps(Settings(tier_weights="T0:2,T3:1").tier_weights)
