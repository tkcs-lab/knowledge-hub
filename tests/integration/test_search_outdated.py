"""search: superseded documents and outdated records are hidden unless include_outdated is true."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.errors import CURSOR_FOREIGN, NORMATIVE
from knowledge_hub.mcp.server import Deps
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt
from tests.support.seed import WHEN, add_chunked_doc, add_chunked_record

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


async def search(mcp_client, caller=alice_all, **args):
    res = await mcp_client.call(caller, "search", args)
    assert not res.is_error, res.text
    return res.data


def superseded_doc(db, text, **fields):
    """A document replaced by another one; returns (old id, successor id)."""
    new = add_chunked_doc(db, "unrelated successor words", embed=False)
    return add_chunked_doc(db, text, superseded_by=new, **fields), new


def outdated_record(db, text, **fields):
    return add_chunked_record(
        db, text, outdated=True, outdated_at=WHEN, outdated_by="alice", replaced_by="", **fields
    )


async def test_superseded_document_is_hidden_by_default(mcp_client, db):
    old, _ = superseded_doc(db, "quokka")
    cur = add_chunked_doc(db, "quokka")
    db.commit()
    out = (await search(mcp_client, query="quokka"))["hits"]
    assert [h["id"] for h in out] == [f"doc:{cur}"]
    assert out[0]["outdated"] is False and out[0]["replaced_by"] == ""
    assert f"doc:{old}" not in [h["id"] for h in out]


async def test_outdated_record_is_hidden_by_default(mcp_client, db):
    outdated_record(db, "wombat")
    cur = add_chunked_record(db, "wombat")
    db.commit()
    out = (await search(mcp_client, query="wombat"))["hits"]
    assert [h["id"] for h in out] == [f"rec:{cur}"]


async def test_include_outdated_returns_them_with_marks(mcp_client, db):
    old, new = superseded_doc(db, "numbat")
    rec = add_chunked_record(
        db, "numbat", outdated=True, outdated_at=WHEN, outdated_by="alice", replaced_by=f"doc:{new}"
    )
    db.commit()
    out = (await search(mcp_client, query="numbat", include_outdated=True))["hits"]
    got = {h["id"]: h for h in out}
    assert set(got) == {f"doc:{old}", f"rec:{rec}"}
    assert (got[f"doc:{old}"]["outdated"], got[f"doc:{old}"]["replaced_by"]) == (True, f"doc:{new}")
    assert (got[f"rec:{rec}"]["outdated"], got[f"rec:{rec}"]["replaced_by"]) == (True, f"doc:{new}")


async def test_included_outdated_hit_is_weighted_like_a_stale_one(mcp_client, db, fresh_db):
    old, _ = superseded_doc(db, "alpha beta")
    db.commit()
    (hit,) = (await search(mcp_client, query="alpha beta", include_outdated=True))["hits"]
    assert hit["id"] == f"doc:{old}" and hit["outdated"] is True
    db.execute("UPDATE kh.documents SET superseded_by = NULL, stale = true WHERE id = %s", (old,))
    db.commit()
    (stale,) = (await search(mcp_client, query="alpha beta"))["hits"]
    assert stale["stale"] is True and stale["id"] == f"doc:{old}"
    assert hit["score"] == pytest.approx(stale["score"])


async def test_hidden_items_do_not_use_up_the_pool(mcp_client, db, app_settings):
    app_settings.search_pool = 1
    old, _ = superseded_doc(db, "dingo dingo dingo")
    second = add_chunked_doc(db, "dingo")
    db.commit()
    out = (await search(mcp_client, query="dingo dingo dingo"))["hits"]
    assert [h["id"] for h in out] == [f"doc:{second}"]


async def test_include_outdated_is_part_of_the_cursor_scope(mcp_client, db):
    for n in range(3):
        add_chunked_doc(db, f"echidna {n}")
    db.commit()
    first = await search(mcp_client, query="echidna", limit=1, include_outdated=True)
    assert first["next_cursor"]
    res = await mcp_client.call(
        alice_all, "search", {"query": "echidna", "limit": 1, "cursor": first["next_cursor"]}
    )
    assert res.is_error and CURSOR_FOREIGN in res.text
    ok = await mcp_client.call(
        alice_all,
        "search",
        {"query": "echidna", "limit": 1, "cursor": first["next_cursor"], "include_outdated": True},
    )
    assert not ok.is_error, ok.text


async def test_min_tier_t0_with_only_superseded_t0_matches_answers_the_normative_text(mcp_client, db):
    superseded_doc(db, "kookaburra")
    add_chunked_record(db, "kookaburra")
    db.commit()
    res = await mcp_client.call(alice_all, "search", {"query": "kookaburra", "min_tier": "T0"})
    assert res.is_error and NORMATIVE in res.text
    ok = await mcp_client.call(
        alice_all, "search", {"query": "kookaburra", "min_tier": "T0", "include_outdated": True}
    )
    assert not ok.is_error, ok.text


async def test_hidden_rows_of_another_group_do_not_change_results(mcp_client, db):
    cur = add_chunked_doc(db, "platypus")
    db.commit()
    base = await search(mcp_client, query="platypus")
    new = add_chunked_doc(db, "other successor", audience="mgmt", repo="kb-mgmt", embed=False)
    add_chunked_doc(db, "platypus", audience="mgmt", repo="kb-mgmt", superseded_by=new)
    outdated_record(db, "platypus", audience="mgmt")
    db.commit()
    again = await search(mcp_client, query="platypus")
    assert again == base
    assert [h["id"] for h in again["hits"]] == [f"doc:{cur}"]
    seen = await search(mcp_client, bob_mgmt, query="platypus", include_outdated=True)
    assert len(seen["hits"]) == 2  # the mgmt caller still finds its own hidden rows on request
