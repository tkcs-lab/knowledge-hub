"""list_datasets, query and save_query through the MCP server (plan 3.6)."""

from __future__ import annotations

import json

import pytest

from knowledge_hub.mcp.errors import (
    AUDIENCE_NOT_HELD,
    BAD_ARG,
    NOT_FOUND,
    QUERY_TIMEOUT,
    UNKNOWN_DATASET,
)
from tests.support.datasets import make_dataset
from tests.support.principals import alice_all, multi, wf_all, wf_mgmt

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def deals(seeded_db, fresh_db):
    return make_dataset(fresh_db)


def row(key, when="2026-11-01T10:00:00Z", **fields):
    return {"key": key, "source_updated_at": when, "fields": fields}


async def upsert(mcp_client, caller, audience, items, **extra):
    res = await mcp_client.call(
        caller,
        "upsert_rows",
        {"dataset": "deals", "audience": audience, "rows": json.dumps(items), **extra},
    )
    assert not res.is_error, res.text
    return res


async def seed(mcp_client):
    await upsert(
        mcp_client,
        wf_all,
        "all",
        [
            row("a1", name="A1", stage="won", amount=10, close_date="2026-01-15"),
            row("a2", name="A2", stage="won", amount=5, close_date="2026-01-20"),
            row("a3", name="A3", stage="open", amount=7, close_date="2026-02-03"),
        ],
    )
    await upsert(
        mcp_client,
        wf_mgmt,
        "mgmt",
        [row("m1", name="M1", stage="open", amount=100, close_date="2026-02-10")],
    )


async def q(mcp_client, caller, **args):
    res = await mcp_client.call(caller, "query", args)
    return res


def ok(res):
    assert not res.is_error, res.text
    return res.data["result"]


def names(result):
    return sorted(r[result["columns"].index("name")] for r in result["rows"])


async def test_audience_scoping(mcp_client):
    await seed(mcp_client)
    got = ok(await q(mcp_client, multi, dataset="deals", select=["name"]))
    assert names(got) == ["A1", "A2", "A3", "M1"]
    got = ok(await q(mcp_client, alice_all, dataset="deals", select=["name"]))
    assert names(got) == ["A1", "A2", "A3"]
    assert got["truncated"] is False and got["dataset"] == "deals"
    got = ok(await q(mcp_client, multi, dataset="deals", select=["name"], audience="mgmt"))
    assert names(got) == ["M1"]
    res = await q(mcp_client, alice_all, dataset="deals", audience="mgmt")
    assert res.is_error and res.text.endswith(AUDIENCE_NOT_HELD)


async def test_group_by_and_metrics(mcp_client):
    await seed(mcp_client)
    got = ok(
        await q(
            mcp_client,
            alice_all,
            dataset="deals",
            group_by=["stage"],
            metrics=["count", "sum:amount"],
            order=["stage"],
        )
    )
    assert got["columns"] == ["stage", "count", "sum_amount"]
    assert got["rows"] == [["open", 1, 7], ["won", 2, 15]]


async def test_time_bucket(mcp_client):
    await seed(mcp_client)
    got = ok(
        await q(mcp_client, alice_all, dataset="deals", time_bucket="close_date:month", metrics=["count"])
    )
    assert got["columns"] == ["bucket", "count"]
    assert got["rows"] == [["2026-01-01", 2], ["2026-02-01", 1]]


async def test_outdated_rows_only_with_flag(mcp_client):
    await seed(mcp_client)
    res = await mcp_client.call(
        wf_all, "delete_rows", {"dataset": "deals", "audience": "all", "keys": ["a1"]}
    )
    assert not res.is_error, res.text
    assert names(ok(await q(mcp_client, alice_all, dataset="deals", select=["name"]))) == ["A2", "A3"]
    got = ok(await q(mcp_client, alice_all, dataset="deals", select=["name"], include_outdated=True))
    assert names(got) == ["A1", "A2", "A3"]


async def test_truncated(mcp_client):
    await seed(mcp_client)
    got = ok(await q(mcp_client, alice_all, dataset="deals", select=["name"], order=["name"], limit=2))
    assert got["rows"] == [["A1"], ["A2"]] and got["truncated"] is True
    got = ok(await q(mcp_client, alice_all, dataset="deals", select=["name"], limit=3))
    assert len(got["rows"]) == 3 and got["truncated"] is False


async def test_saved_query_reads_only_its_audience(mcp_client):
    await seed(mcp_client)
    res = await mcp_client.call(
        multi,
        "save_query",
        {"name": "by-name", "audience": "all", "dataset": "deals", "select": ["name"], "order": ["name"]},
    )
    assert ok(res) == {"audience": "all", "name": "by-name"}
    got = ok(await q(mcp_client, multi, saved="by-name", audience="all"))
    assert names(got) == ["A1", "A2", "A3"]
    # saving the same name again replaces it
    await mcp_client.call(
        multi,
        "save_query",
        {"name": "by-name", "audience": "all", "dataset": "deals", "select": ["name"], "limit": 1},
    )
    assert len(ok(await q(mcp_client, multi, saved="by-name", audience="all"))["rows"]) == 1
    res = await mcp_client.call(
        alice_all, "save_query", {"name": "x", "audience": "mgmt", "dataset": "deals"}
    )
    assert res.is_error and res.text.endswith(AUDIENCE_NOT_HELD)
    res = await mcp_client.call(
        multi, "save_query", {"name": "Bad Name", "audience": "all", "dataset": "deals"}
    )
    assert res.is_error and res.text.endswith(BAD_ARG.format(name="name"))
    res = await mcp_client.call(
        multi, "save_query", {"name": "x", "audience": "all", "dataset": "nope"}
    )
    assert res.is_error and res.text.endswith(UNKNOWN_DATASET)


async def test_query_argument_errors(mcp_client):
    await seed(mcp_client)
    res = await q(mcp_client, multi, saved="nope", audience="all")
    assert res.is_error and res.text.endswith(NOT_FOUND)
    await mcp_client.call(
        multi, "save_query", {"name": "s", "audience": "all", "dataset": "deals", "select": ["name"]}
    )
    res = await q(mcp_client, multi, saved="s", audience="all", select=["name"])
    assert res.is_error and res.text.endswith(BAD_ARG.format(name="saved"))
    res = await q(mcp_client, multi)
    assert res.is_error and res.text.endswith(BAD_ARG.format(name="dataset"))
    res = await q(mcp_client, multi, dataset="deals", as_of="2026-01-01")
    assert res.is_error and res.text.endswith(BAD_ARG.format(name="as_of"))
    res = await q(mcp_client, multi, saved="s")
    assert res.is_error and res.text.endswith(BAD_ARG.format(name="audience"))
    res = await q(mcp_client, multi, dataset="nope")
    assert res.is_error and res.text.endswith(UNKNOWN_DATASET)


async def test_list_datasets(mcp_client, fresh_db):
    make_dataset(fresh_db, name="hr_only", audiences=("hr",), columns=(), title_column="")
    await seed(mcp_client)
    await upsert(
        mcp_client,
        wf_all,
        "all",
        [row("a1", name="A1"), row("a2", name="A2"), row("a3", name="A3")],
        sync_id="s2",
    )
    res = await mcp_client.call(
        wf_all, "finish_sync", {"dataset": "deals", "audience": "all", "sync_id": "s2"}
    )
    assert not res.is_error, res.text
    got = ok(await mcp_client.call(alice_all, "list_datasets", {}))
    assert [d["name"] for d in got["datasets"]] == ["accounts", "deals"]
    deal = got["datasets"][1]
    assert deal["audiences"] == ["all"]
    assert "rows" not in deal
    assert deal["watermark"].startswith("2026-11-01")
    assert [s["sync_id"] for s in deal["last_syncs"]] == ["s2"]
    assert {c["name"] for c in deal["columns"]} >= {"name", "amount"}
    got = ok(await mcp_client.call(multi, "list_datasets", {"dataset": "deals"}))
    deal = got["datasets"][0]
    assert deal["audiences"] == ["all", "mgmt"]
    assert deal["rows"] == 4 and deal["rows_capped"] is False
    res = await mcp_client.call(alice_all, "list_datasets", {"dataset": "hr_only"})
    assert res.is_error and res.text.endswith(UNKNOWN_DATASET)


async def test_list_datasets_saved_queries(mcp_client):
    await mcp_client.call(multi, "save_query", {"name": "s", "audience": "mgmt", "dataset": "deals"})
    got = ok(await mcp_client.call(multi, "list_datasets", {}))
    assert got["saved_queries"] == [{"audience": "mgmt", "name": "s", "dataset": "deals"}]
    got = ok(await mcp_client.call(alice_all, "list_datasets", {}))
    assert got["saved_queries"] == []


async def test_timeout(mcp_client, deps, monkeypatch):
    for start in range(0, 20000, 500):
        await upsert(
            mcp_client,
            wf_all,
            "all",
            [row(f"k{i}", name=f"N{i}", amount=i) for i in range(start, start + 500)],
        )
    monkeypatch.setattr(deps.settings, "query_timeout_ms", 1)
    res = await q(mcp_client, alice_all, dataset="deals", group_by=["name"], metrics=["sum:amount"])
    assert res.is_error and res.text.endswith(QUERY_TIMEOUT), res.text
