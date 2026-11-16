"""Search never shows, counts or dates a row the caller may not see (plan 3.5, 3.3)."""

from datetime import UTC, datetime, timedelta

import pytest

from knowledge_hub.visibility import Visibility, can_read
from tests.integration.test_search_ranking import (  # noqa: F401  (fixtures)
    add_derived,
    add_doc,
    add_raw,
    app_settings,
    deps,
    ids,
)
from tests.support.principals import PRINCIPALS
from tests.support.seed import CASES

QUERY = "leak probe sentence"
BASE = datetime(2026, 3, 1, tzinfo=UTC)


def visible(caller, case: str) -> bool:
    return can_read(Visibility(*CASES[case]), caller.audiences, caller.email)


@pytest.fixture
async def world(indexer_pool, deps):  # noqa: F811
    """One T0 document, one T2 and one T3 record per visibility case; later cases are newer."""
    rows: dict[str, dict[str, object]] = {}
    for n, (case, (aud, emails)) in enumerate(CASES.items()):
        when = BASE + timedelta(days=n)
        text = f"{QUERY} {case}"
        rows[case] = {
            "T0": await add_doc(indexer_pool, text, audiences=tuple(aud), emails=emails),
            "T2": await add_derived(indexer_pool, text, audiences=tuple(aud), emails=emails, created_at=when),
            "T3": await add_raw(indexer_pool, text, audiences=tuple(aud), emails=emails, ingested_at=when),
            "when": when,
        }
    return rows


@pytest.mark.parametrize("principal", sorted(PRINCIPALS))
async def test_hits_are_exactly_the_visible_citation_ids(world, mcp_client, principal):
    caller = PRINCIPALS[principal]
    res = await mcp_client.call(caller, "search", {"query": QUERY, "min_tier": "T3", "limit": 50})
    assert not res.is_error, res.text
    expected = {r[t] for c, r in world.items() if visible(caller, c) for t in ("T0", "T2", "T3")}
    assert set(ids(res)) == expected
    assert len(res.data["hits"]) == len(expected)  # hidden matches do not change the count


@pytest.mark.parametrize("principal", sorted(PRINCIPALS))
async def test_lower_tier_counts_and_newest_at_ignore_hidden_rows(world, mcp_client, principal):
    caller = PRINCIPALS[principal]
    res = await mcp_client.call(caller, "search", {"query": QUERY, "min_tier": "T0", "limit": 50})
    assert not res.is_error, res.text
    seen = [r for c, r in world.items() if visible(caller, c)]
    lower = res.data["lower_tier_hits"]
    assert lower["T2"] == len(seen) and lower["T3"] == len(seen)
    newest = max((r["when"] for r in seen), default=None)
    assert lower["newest_at"] == (newest.isoformat() if newest else None)
    assert set(ids(res)) == {r["T0"] for r in seen}


async def test_lower_tier_summary_is_empty_when_nothing_is_below(world, mcp_client):
    alice = PRINCIPALS["alice_all"]
    res = await mcp_client.call(alice, "search", {"query": QUERY, "min_tier": "T3"})
    assert res.data["lower_tier_hits"] == {"T2": 0, "T3": 0, "newest_at": None}


async def test_cursor_from_another_principal_is_refused(world, mcp_client):
    cursors = {}
    for name, caller in PRINCIPALS.items():
        res = await mcp_client.call(caller, "search", {"query": QUERY, "min_tier": "T3", "limit": 1})
        assert not res.is_error, res.text
        if res.data["next_cursor"]:
            cursors[name] = res.data["next_cursor"]
    assert cursors, "no principal has a second page"
    for owner, token in cursors.items():
        for name, caller in PRINCIPALS.items():
            res = await mcp_client.call(
                caller, "search", {"query": QUERY, "min_tier": "T3", "limit": 1, "cursor": token}
            )
            if name == owner:
                assert not res.is_error, res.text
            elif caller.sub == PRINCIPALS[owner].sub and caller.audiences == PRINCIPALS[owner].audiences:
                continue
            else:
                assert res.is_error and res.text.endswith("cursor does not belong to this query")


async def test_cursor_is_bound_to_its_query_and_filters(world, mcp_client):
    alice = PRINCIPALS["alice_all"]
    first = await mcp_client.call(alice, "search", {"query": QUERY, "min_tier": "T3", "limit": 1})
    token = first.data["next_cursor"]
    for args in (
        {"query": QUERY + " more", "min_tier": "T3"},
        {"query": QUERY, "min_tier": "T2"},
        {"query": QUERY, "min_tier": "T3", "domains": ["product"]},
    ):
        res = await mcp_client.call(alice, "search", {**args, "limit": 1, "cursor": token})
        assert res.is_error and res.text.endswith("cursor does not belong to this query"), args
    for bad in ("garbage", "AAAA", token[:-2] + "AA"):
        res = await mcp_client.call(alice, "search", {"query": QUERY, "min_tier": "T3", "cursor": bad})
        assert res.is_error and res.text.endswith("cursor does not belong to this query")
