"""Usage events from search and get_doc: what is written, for whom, and that a failure never shows."""

from __future__ import annotations

import re

import psycopg
import pytest
from psycopg.rows import dict_row

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.errors import NORMATIVE
from knowledge_hub.mcp.server import Deps
from knowledge_hub.metrics import METRICS
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, dave_none, wf_token
from tests.support.seed import add_chunked_doc, add_chunked_record

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def fresh_metrics():
    METRICS.reset()
    yield
    METRICS.reset()


@pytest.fixture
def app_settings(db_settings, tmp_path):
    from tests.support.mcp_client import HOST

    key = tmp_path / "cursor.key"
    key.write_bytes(b"k" * 32)
    return db_settings.model_copy(update={"env": "test", "allowed_hosts": HOST, "cursor_key_file": key})


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


def events(fresh_db) -> list[dict]:
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), row_factory=dict_row) as conn:
        return conn.execute("SELECT * FROM kh.usage_events ORDER BY at, id").fetchall()


async def test_search_writes_one_event(mcp_client, db, fresh_db):
    doc = add_chunked_doc(db, "wombat handbook")
    rec = add_chunked_record(db, "wombat ledger")
    db.commit()
    res = await mcp_client.call(alice_all, "search", {"query": "wombat"})
    assert not res.is_error, res.text
    rows = events(fresh_db)
    assert len(rows) == 1
    row = rows[0]
    assert (row["kind"], row["query"], row["min_tier"]) == ("search", "wombat", "T3")
    assert row["audiences"] == ["all"]
    assert row["owner_keys"] == [h["id"] for h in res.data["hits"]]
    assert set(row["owner_keys"]) == {f"doc:{doc}", f"rec:{rec}"}
    assert re.fullmatch(r"[0-9a-f]{32}", row["who"]) and row["who"] != "alice"


@pytest.mark.parametrize("caller", [wf_token, dave_none])
async def test_other_callers_write_nothing(mcp_client, db, fresh_db, caller):
    add_chunked_doc(db, "wombat handbook")
    db.commit()
    await mcp_client.call(caller, "search", {"query": "wombat"})
    assert events(fresh_db) == []


async def test_usage_log_off_writes_nothing(mcp_client, deps, db, fresh_db):
    deps.settings = deps.settings.model_copy(update={"usage_log": False})
    add_chunked_doc(db, "wombat handbook")
    db.commit()
    res = await mcp_client.call(alice_all, "search", {"query": "wombat"})
    assert not res.is_error, res.text
    assert events(fresh_db) == []


async def test_second_page_writes_no_new_row(mcp_client, db, fresh_db):
    add_chunked_doc(db, "numbat one")
    add_chunked_doc(db, "numbat two")
    db.commit()
    first = await mcp_client.call(alice_all, "search", {"query": "numbat", "limit": 1})
    assert first.data["next_cursor"]
    assert len(events(fresh_db)) == 1
    second = await mcp_client.call(
        alice_all, "search", {"query": "numbat", "limit": 1, "cursor": first.data["next_cursor"]}
    )
    assert not second.is_error, second.text
    assert len(events(fresh_db)) == 1


async def test_get_doc_writes_an_open_event(mcp_client, seeded_db, fresh_db):
    key = seeded_db["documents"]["all"]
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{key}"})
    assert not res.is_error, res.text
    rows = events(fresh_db)
    assert len(rows) == 1
    assert (rows[0]["kind"], rows[0]["owner_keys"], rows[0]["query"]) == ("open", [f"doc:{key}"], None)


async def test_get_doc_of_hidden_or_unknown_writes_nothing(mcp_client, seeded_db, fresh_db):
    hidden = seeded_db["documents"]["hr"]
    for ident in (f"doc:{hidden}", "doc:00000000-0000-0000-0000-000000000000"):
        res = await mcp_client.call(alice_all, "get_doc", {"id": ident})
        assert res.is_error
    assert events(fresh_db) == []


async def test_normative_failure_is_still_logged(mcp_client, db, fresh_db):
    add_chunked_record(db, "platypus ledger")
    db.commit()
    res = await mcp_client.call(alice_all, "search", {"query": "platypus", "min_tier": "T0"})
    assert res.is_error and res.text.endswith(NORMATIVE)
    rows = events(fresh_db)
    assert len(rows) == 1
    assert (rows[0]["min_tier"], rows[0]["owner_keys"]) == ("T0", [])


async def test_fts_signals_split_by_tier(mcp_client, db, fresh_db):
    add_chunked_doc(db, "zebrafinch only in documents")
    add_chunked_record(db, "axolotl only in records")
    db.commit()
    for query in ("zebrafinch", "axolotl", "nobodyhasthisword"):
        await mcp_client.call(alice_all, "search", {"query": query})
    got = {r["query"]: (r["t0_fts_hits"], r["t3_fts_hits"]) for r in events(fresh_db)}
    assert got == {"zebrafinch": (1, 0), "axolotl": (0, 1), "nobodyhasthisword": (0, 0)}


async def test_a_failing_write_does_not_break_the_search(mcp_client, db, monkeypatch):
    doc = add_chunked_doc(db, "wombat handbook")
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr("knowledge_hub.usage.caller_tx", boom)
    res = await mcp_client.call(alice_all, "search", {"query": "wombat"})
    assert not res.is_error, res.text
    assert [h["id"] for h in res.data["hits"]] == [f"doc:{doc}"]
    assert "kh_usage_events_dropped_total 1" in METRICS.render()
