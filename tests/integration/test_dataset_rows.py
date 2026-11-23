"""upsert_rows, delete_rows and finish_sync through the MCP server (plan 3.3, 3.5)."""

from __future__ import annotations

import json
import time

import psycopg
import pytest

from knowledge_hub.mcp.errors import (
    AUDIENCE_NOT_HELD,
    BAD_ARG,
    ONLY_WORKFLOWS,
    SYNC_SHRINK,
    TOO_MANY_ROWS,
    UNKNOWN_DATASET,
)
from tests.support.datasets import make_dataset
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, wf_all, wf_hr, wf_mgmt

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def deals(seeded_db, fresh_db):
    return make_dataset(fresh_db)


def rows(*items) -> str:
    return json.dumps(list(items))


def row(key, when="2026-11-01T10:00:00Z", **fields):
    return {"key": key, "source_updated_at": when, "fields": fields}


def q(fresh_db, sql, *params):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        return conn.execute(sql, params).fetchall()


async def upsert(mcp_client, items, caller=wf_all, audience="all", **extra):
    return await mcp_client.call(
        caller, "upsert_rows", {"dataset": "deals", "audience": audience, "rows": rows(*items), **extra}
    )


async def test_insert_then_update_counts(mcp_client, fresh_db):
    res = await upsert(mcp_client, [row("a", name="A", amount=1), row("b", name="B", amount="2.5")])
    assert not res.is_error, res.text
    assert res.data == {"rows": 2, "inserted": 2, "updated": 0}
    res = await upsert(
        mcp_client,
        [row("a", "2026-11-02T10:00:00Z", name="A2", amount=3), row("c", name="C", amount=4)],
    )
    assert res.data == {"rows": 2, "inserted": 1, "updated": 1}
    got = q(fresh_db, "SELECT _key, name, amount FROM kh_data.deals ORDER BY _key")
    assert [(k, n, float(a)) for k, n, a in got] == [("a", "A2", 3.0), ("b", "B", 2.5), ("c", "C", 4.0)]


async def test_every_column_type_is_stored(mcp_client, fresh_db):
    res = await upsert(
        mcp_client,
        [row("a", name="A", stage="won", amount="12.50", close_date="2026-11-01T10:00:00Z")],
    )
    assert not res.is_error, res.text
    ((name, stage, amount, close),) = q(fresh_db, "SELECT name, stage, amount, close_date FROM kh_data.deals")
    assert (name, stage, str(amount), str(close)) == ("A", "won", "12.50", "2026-11-01")


async def test_older_timestamp_keeps_values_but_marks_seen(mcp_client, fresh_db):
    await upsert(mcp_client, [row("a", "2026-11-05T00:00:00Z", name="new")])
    (before,) = q(fresh_db, "SELECT _seen_at FROM kh_data.deals")
    time.sleep(0.01)
    res = await upsert(mcp_client, [row("a", "2026-11-01T00:00:00Z", name="old")])
    assert not res.is_error, res.text
    ((name, seen, updated),) = q(fresh_db, "SELECT name, _seen_at, _source_updated_at FROM kh_data.deals")
    assert name == "new"
    assert seen > before[0]
    assert updated.isoformat().startswith("2026-11-05")


async def test_omitted_field_keeps_its_value(mcp_client, fresh_db):
    await upsert(mcp_client, [row("a", name="A", amount=5)])
    await upsert(mcp_client, [row("a", "2026-11-02T00:00:00Z", name="B")])
    ((name, amount),) = q(fresh_db, "SELECT name, amount FROM kh_data.deals")
    assert (name, float(amount)) == ("B", 5.0)


async def test_refusals(mcp_client):
    res = await upsert(mcp_client, [row("a")], caller=alice_all)
    assert res.is_error and res.text == ONLY_WORKFLOWS
    res = await upsert(mcp_client, [row("a")], caller=wf_mgmt, audience="hr")
    assert res.is_error and res.text == AUDIENCE_NOT_HELD
    res = await mcp_client.call(
        wf_all, "upsert_rows", {"dataset": "nope", "audience": "all", "rows": rows(row("a"))}
    )
    assert res.is_error and res.text == UNKNOWN_DATASET
    res = await upsert(mcp_client, [row("a")], caller=wf_hr, audience="hr")
    assert res.is_error and res.text == UNKNOWN_DATASET
    res = await upsert(mcp_client, [row("a")], sync_id="bad id!")
    assert res.is_error and res.text == BAD_ARG.format(name="sync_id")
    res = await upsert(mcp_client, [row("a", nope=1)])
    assert res.is_error and res.text == "row 0: unknown field nope"
    res = await mcp_client.call(
        wf_all, "upsert_rows", {"dataset": "deals", "audience": "all", "rows": "[]"}
    )
    assert res.is_error and res.text == BAD_ARG.format(name="rows")
    res = await upsert(mcp_client, [row(str(i)) for i in range(501)])
    assert res.is_error and res.text == TOO_MANY_ROWS


async def test_delete_rows(mcp_client, fresh_db):
    await upsert(mcp_client, [row("a", "2026-11-05T00:00:00Z", name="A"), row("b", name="B")])
    res = await mcp_client.call(
        wf_all, "delete_rows", {"dataset": "deals", "audience": "all", "keys": ["a", "b", "zzz"]}
    )
    assert res.data == {"outdated": 2}
    rows_ = q(fresh_db, "SELECT _key, _outdated, _outdated_reason FROM kh_data.deals ORDER BY _key")
    assert rows_ == [("a", True, "delete"), ("b", True, "delete")]
    # an older re-send keeps the delete, an equal timestamp clears it
    await upsert(mcp_client, [row("a", "2026-11-01T00:00:00Z", name="A")])
    assert q(fresh_db, "SELECT _outdated FROM kh_data.deals WHERE _key = 'a'") == [(True,)]
    await upsert(mcp_client, [row("a", "2026-11-05T00:00:00Z", name="A")])
    cleared = q(fresh_db, "SELECT _outdated, _outdated_reason, _outdated_at FROM kh_data.deals WHERE _key='a'")
    assert cleared == [(False, "", None)]
    res = await mcp_client.call(
        wf_all, "delete_rows", {"dataset": "deals", "audience": "all", "keys": ["a"]}
    )
    assert res.data == {"outdated": 1}


async def test_delete_rows_arguments(mcp_client):
    base = {"dataset": "deals", "audience": "all"}
    res = await mcp_client.call(wf_all, "delete_rows", {**base, "keys": []})
    assert res.is_error and res.text == BAD_ARG.format(name="keys")
    res = await mcp_client.call(wf_all, "delete_rows", {**base, "keys": [""]})
    assert res.is_error and res.text == BAD_ARG.format(name="keys")
    res = await mcp_client.call(wf_all, "delete_rows", {**base, "keys": [str(i) for i in range(501)]})
    assert res.is_error and res.text == TOO_MANY_ROWS
    res = await mcp_client.call(alice_all, "delete_rows", {**base, "keys": ["a"]})
    assert res.is_error and res.text == ONLY_WORKFLOWS


async def test_full_sync_outdates_only_rows_not_seen(mcp_client, fresh_db):
    await upsert(mcp_client, [row("E", name="E")])
    time.sleep(0.05)
    res = await upsert(mcp_client, [row("A", name="A"), row("B", name="B"), row("C", name="C")], sync_id="s1")
    assert not res.is_error, res.text
    res = await upsert(mcp_client, [row("D", name="D")])  # incremental run in between
    assert not res.is_error, res.text
    res = await mcp_client.call(
        wf_all, "finish_sync", {"dataset": "deals", "audience": "all", "sync_id": "s1", "force": True}
    )
    assert not res.is_error, res.text
    assert res.data["outdated"] == 1 and res.data["live"] == 4
    assert res.data["watermark"].startswith("2026-11-01T10:00:00")
    live = dict(q(fresh_db, "SELECT _key, _outdated FROM kh_data.deals"))
    assert live == {"A": False, "B": False, "C": False, "D": False, "E": True}
    ((reason,),) = q(fresh_db, "SELECT _outdated_reason FROM kh_data.deals WHERE _key = 'E'")
    assert reason == "sync"
    ((rows_seen, finished, outdated, mark),) = q(
        fresh_db, "SELECT rows_seen, finished_at, outdated, watermark FROM kh.dataset_syncs"
    )
    assert (rows_seen, outdated) == (3, 1) and finished is not None and mark is not None
    # a finished sync takes no more rows and cannot finish twice
    res = await upsert(mcp_client, [row("A", name="A")], sync_id="s1")
    assert res.is_error and res.text == BAD_ARG.format(name="sync_id")
    res = await mcp_client.call(
        wf_all, "finish_sync", {"dataset": "deals", "audience": "all", "sync_id": "s1"}
    )
    assert res.is_error and res.text == BAD_ARG.format(name="sync_id")


async def test_shrink_guard(mcp_client, fresh_db):
    await upsert(mcp_client, [row(k, name=k) for k in "ABCDE"])
    time.sleep(0.05)
    await upsert(mcp_client, [row("A", name="A"), row("B", name="B")], sync_id="s2")
    args = {"dataset": "deals", "audience": "all", "sync_id": "s2"}
    res = await mcp_client.call(wf_all, "finish_sync", args)
    assert res.is_error
    assert res.text == SYNC_SHRINK.format(n=3, live=5, percent=20)
    assert q(fresh_db, "SELECT count(*) FROM kh_data.deals WHERE _outdated") == [(0,)]
    res = await mcp_client.call(wf_all, "finish_sync", {**args, "force": True})
    assert res.data["outdated"] == 3


async def test_finish_sync_arguments(mcp_client):
    base = {"dataset": "deals", "audience": "all"}
    res = await mcp_client.call(wf_all, "finish_sync", {**base, "sync_id": ""})
    assert res.is_error and res.text == BAD_ARG.format(name="sync_id")
    res = await mcp_client.call(wf_all, "finish_sync", {**base, "sync_id": "unknown"})
    assert res.is_error and res.text == BAD_ARG.format(name="sync_id")
    res = await mcp_client.call(alice_all, "finish_sync", {**base, "sync_id": "x"})
    assert res.is_error and res.text == ONLY_WORKFLOWS
