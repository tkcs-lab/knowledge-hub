"""usage: ranking by distinct people, the two-person floor, the subset rule, both views."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.support.pg import FRESH_DB
from tests.support.principals import KH_CONFIG, alice_all, multi, wf_token
from tests.support.seed import add_doc, add_record, add_usage

pytestmark = pytest.mark.integration


def who(n: int) -> str:
    return f"{n:032x}"


@pytest.fixture
def db(fresh_db, seeded_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


async def records(mcp_client, caller=alice_all, **args):
    res = await mcp_client.call(caller, "usage", {"view": "records", **args})
    assert not res.is_error, res.text
    return res.data


def hit(db, rec, n, **fields):
    return add_usage(db, who=who(n), owner_keys=[f"rec:{rec}"], **fields)


async def test_records_rank_by_distinct_people_first(mcp_client, db):
    wide, busy = add_record(db, title="wide"), add_record(db, title="busy")
    for n in (1, 2, 3):
        hit(db, wide, n)
    for n in (1, 2):
        for _ in range(5):
            hit(db, busy, n)
    data = await records(mcp_client)
    assert [i["title"] for i in data["items"]] == ["wide", "busy"]
    assert (data["items"][0]["people"], data["items"][1]["people"]) == (3, 2)
    assert data["items"][1]["searches"] == 10


async def test_one_person_is_hidden_and_min_people_is_clamped(mcp_client, db):
    solo = add_record(db, title="solo")
    pair = add_record(db, title="pair")
    for _ in range(4):
        hit(db, solo, 1, query="alone")
    hit(db, pair, 1, query="shared q")
    hit(db, pair, 2, query="shared q")
    hit(db, pair, 1, query="private q")
    for min_people in (2, 1, 0):
        data = await records(mcp_client, min_people=min_people)
        assert data["min_people"] == 2
        assert [i["title"] for i in data["items"]] == ["pair"]
    assert data["items"][0]["sample_queries"] == ["shared q"]


async def test_outranked_t0_needs_no_doc_before_the_record(mcp_client, db):
    a = add_record(db, title="A")
    d = f"doc:{add_doc(db)}"
    add_usage(db, who=who(1), owner_keys=[f"rec:{a}", d])
    add_usage(db, who=who(2), owner_keys=[d, f"rec:{a}"])
    data = await records(mcp_client)
    assert data["items"][0]["outranked_t0"] == 1
    assert data["items"][0]["searches"] == 2


async def test_t0a_document_ranked_first_does_not_stop_outranked_t0(mcp_client, db):
    a = add_record(db, title="A")
    t0a = f"doc:{add_doc(db, tier='T0a')}"
    for n in (1, 2):
        add_usage(db, who=who(n), owner_keys=[t0a, f"rec:{a}"])
    assert (await records(mcp_client))["items"][0]["outranked_t0"] == 2


async def test_related_docs_lists_only_t0_documents(mcp_client, db):
    rec = add_record(db)
    t0, t0a = add_doc(db), add_doc(db, tier="T0a")
    for n in (1, 2):
        add_usage(db, who=who(n), owner_keys=[f"rec:{rec}", f"doc:{t0}", f"doc:{t0a}"])
    assert (await records(mcp_client))["items"][0]["related_docs"] == [f"doc:{t0}"]


async def test_related_docs_and_sample_queries_are_capped_and_sorted(mcp_client, db):
    rec = add_record(db)
    docs = sorted(add_doc(db, path=f"d{i}.md") for i in range(4))
    hidden = add_doc(db, audience="hr", repo=KH_CONFIG.repo_for_group("hr"), path="hr2.md")
    for n in (1, 2):
        for q in ("Delta", "alpha", "charlie", "bravo"):
            add_usage(
                db, who=who(n), query=q,
                owner_keys=[f"rec:{rec}", *[f"doc:{d}" for d in docs], f"doc:{hidden}", "doc:ghost"],
            )
    item = (await records(mcp_client))["items"][0]
    assert item["related_docs"] == [f"doc:{d}" for d in docs[:3]]
    assert item["sample_queries"] == ["alpha", "bravo", "charlie"]


async def test_window_and_days_clamp(mcp_client, db, deps):
    rec = add_record(db)
    old = datetime.now(UTC) - timedelta(days=40)
    hit(db, rec, 1, at=old)
    hit(db, rec, 2, at=old)
    hit(db, rec, 3)
    assert (await records(mcp_client))["items"] == []
    assert len((await records(mcp_client, days=60))["items"]) == 1
    deps.settings = deps.settings.model_copy(update={"usage_retention_days": 7})
    assert (await records(mcp_client, days=1000))["days"] == 7


async def test_subset_rule_through_the_tool(mcp_client, db):
    rec = add_record(db, title="x")
    for n in (1, 2):
        hit(db, rec, n, audiences=["all", "mgmt"])
    assert (await records(mcp_client, alice_all))["items"] == []
    assert len((await records(mcp_client, multi))["items"]) == 1
    rec2 = add_record(db, title="y")
    for n in (1, 2):
        hit(db, rec2, n, audiences=["all"])
    items = (await records(mcp_client, wf_token))["items"]
    assert [i["title"] for i in items] == ["y"]


async def test_audience_filter_keeps_records_of_that_audience(mcp_client, db):
    a = add_record(db, title="in all", audience="all")
    m = add_record(db, title="in mgmt", audience="mgmt")
    for rec in (a, m):
        for n in (1, 2):
            hit(db, rec, n)
    items = (await records(mcp_client, multi, audience="mgmt"))["items"]
    assert [i["title"] for i in items] == ["in mgmt"]


async def test_queries_view(mcp_client, db):
    for n, tier, t3 in ((1, "T3", 2), (2, "T0", 5), (3, "T3", 1)):
        add_usage(db, who=who(n), query="  Gap   Query ", min_tier=tier, t3_fts_hits=t3)
    add_usage(db, who=who(1), query="answered", t0_fts_hits=1)
    add_usage(db, who=who(1), query="one man", audiences=["all"])
    add_usage(db, who=who(1), query="mgmt only", audiences=["all", "mgmt"])
    add_usage(db, who=who(2), query="mgmt only", audiences=["all", "mgmt"])
    res = await mcp_client.call(alice_all, "usage", {"view": "queries"})
    assert not res.is_error, res.text
    [item] = res.data["items"]
    assert (item["query"], item["searches"], item["people"]) == ("gap query", 3, 3)
    assert (item["t3_matches"], item["asked_for_t0"]) == (5, True)
    assert item["last_seen"]
    res = await mcp_client.call(alice_all, "usage", {"view": "queries", "min_people": 4})
    assert res.data["items"] == []
    res = await mcp_client.call(multi, "usage", {"view": "queries", "audience": "mgmt"})
    assert [i["query"] for i in res.data["items"]] == ["mgmt only"]
    res = await mcp_client.call(multi, "usage", {"view": "queries", "audience": "hr"})
    assert res.data["items"] == []


async def test_no_item_exposes_who_or_audiences(mcp_client, db):
    rec = add_record(db)
    for n in (1, 2):
        hit(db, rec, n, query="q")
        add_usage(db, who=who(n), query="gap")
    for view in ("records", "queries"):
        res = await mcp_client.call(alice_all, "usage", {"view": view})
        assert res.data["items"]
        for item in res.data["items"]:
            assert "who" not in item and "audiences" not in item


async def test_bad_view_is_an_error_and_limit_is_clamped(mcp_client, db):
    res = await mcp_client.call(alice_all, "usage", {"view": "nonsense"})
    assert res.is_error
    for i in range(2):
        rec = add_record(db, title=f"r{i}")
        for n in (1, 2):
            hit(db, rec, n)
    assert len((await records(mcp_client, limit=0))["items"]) == 1


async def test_the_tool_writes_no_usage_event(mcp_client, db):
    add_usage(db, who=who(1))
    before = db.execute("SELECT count(*) FROM kh.usage_events").fetchone()[0]
    await records(mcp_client)
    await mcp_client.call(alice_all, "usage", {"view": "queries"})
    assert db.execute("SELECT count(*) FROM kh.usage_events").fetchone()[0] == before
