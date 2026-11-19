"""search never returns a hit of an audience the caller lacks: documents, records and chunks."""

from __future__ import annotations

import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.errors import NORMATIVE
from knowledge_hub.mcp.server import Deps
from tests.support.mcp_client import HOST
from tests.support.principals import (
    KH_CONFIG,
    alice_all,
    bob_mgmt,
    carol_hr,
    dave_none,
    multi,
    wf_token,
)

pytestmark = pytest.mark.integration

CALLERS = [alice_all, bob_mgmt, carol_hr, dave_none, multi, wf_token]


@pytest.fixture
def app_settings(db_settings, tmp_path):
    key = tmp_path / "cursor.key"
    key.write_bytes(b"k" * 32)
    return db_settings.model_copy(update={"env": "test", "allowed_hosts": HOST, "cursor_key_file": key})


@pytest.fixture
def deps(app_settings, kh_config, app_pool, indexer_pool) -> Deps:
    return Deps(
        settings=app_settings,
        config=kh_config,
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        embedder=FakeEmbedder(),
    )


def expected(ids, caller):
    """Owner ids of the seed rows (one document and one record per group) the caller may read."""
    return {
        f"{prefix}:{ids[table][group]}"
        for table, prefix in (("documents", "doc"), ("records", "rec"))
        for group in KH_CONFIG.groups
        if group in caller.audiences
    }


@pytest.mark.parametrize("caller", CALLERS, ids=lambda c: c.sub)
@pytest.mark.parametrize("query", ["chunk", "Doc Rec"])
async def test_a_caller_sees_only_held_audiences(mcp_client, seeded_db, caller, query):
    res = await mcp_client.call(caller, "search", {"query": query, "min_tier": "T3", "limit": 50})
    assert not res.is_error, res.text
    got = res.data["hits"]
    assert {h["audience"] for h in got} <= set(caller.audiences)
    if query == "chunk":  # every seeded chunk text holds this word
        assert {h["id"] for h in got} == expected(seeded_db, caller)


@pytest.mark.parametrize("caller", CALLERS, ids=lambda c: c.sub)
async def test_documents_only_search_sees_only_held_documents(mcp_client, seeded_db, caller):
    res = await mcp_client.call(caller, "search", {"query": "chunk", "limit": 50, "min_tier": "T0"})
    if not caller.audiences:
        assert not res.is_error and res.data["hits"] == []
        return
    assert not res.is_error, res.text
    assert {h["id"] for h in res.data["hits"]} == {
        f"doc:{seeded_db['documents'][g]}" for g in KH_CONFIG.groups if g in caller.audiences
    }


async def test_dave_none_sees_nothing_anywhere(mcp_client, seeded_db):
    assert dave_none.audiences == frozenset()
    for tier in ("T0", "T3"):
        for query in ("chunk", "doc", "rec", "Doc all", "hr"):
            res = await mcp_client.call(dave_none, "search", {"query": query, "min_tier": tier})
            assert not res.is_error, res.text  # not even NORMATIVE: records are invisible too
            assert res.data["hits"] == []


async def test_naming_a_foreign_audience_reveals_nothing(mcp_client, seeded_db):
    for foreign in ("hr", "mgmt", "nonexistent"):
        args = {"query": "chunk", "min_tier": "T3", "audience": foreign}
        res = await mcp_client.call(alice_all, "search", args)
        assert not res.is_error and res.data["hits"] == []


async def test_a_foreign_record_does_not_trigger_normative(mcp_client, seeded_db):
    # bob's mgmt record matches, but alice cannot see it: she gets an empty result, not NORMATIVE
    res = await mcp_client.call(alice_all, "search", {"query": "chunk of record mgmt", "min_tier": "T0"})
    assert not res.is_error, res.text
    assert not res.text.endswith(NORMATIVE)
    assert all(h["audience"] == "all" for h in res.data["hits"])
