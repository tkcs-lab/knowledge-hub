"""The four read tools end to end through the HTTP app with signed claims."""

from __future__ import annotations

import pytest

from knowledge_hub.embed.base import FakeEmbedder
from tests.support.claims import mint_claim
from tests.support.mcp_client import HOST
from tests.support.principals import KH_CONFIG, alice_all, carol_hr

pytestmark = pytest.mark.integration


@pytest.fixture
def app_settings(db_settings, tmp_path):
    key = tmp_path / "cursor.key"
    key.write_bytes(b"k" * 32)
    return db_settings.model_copy(update={"env": "test", "allowed_hosts": HOST, "cursor_key_file": key})


@pytest.fixture
def deps(deps):
    deps.embedder = FakeEmbedder()
    return deps


async def test_each_read_tool_answers_through_the_http_app(mcp_client, seeded_db):
    doc = f"doc:{seeded_db['documents']['all']}"
    found = await mcp_client.call(alice_all, "search", {"query": "chunk"})
    assert not found.is_error, found.text
    assert [h["id"] for h in found.data["hits"]] == [doc]
    got = await mcp_client.call(alice_all, "get_doc", {"id": doc})
    assert not got.is_error and got.data["title"] == "Doc all"
    stale = await mcp_client.call(alice_all, "list_stale")
    assert not stale.is_error and stale.data["documents"] == []
    labels = await mcp_client.call(alice_all, "list_labels")
    assert not labels.is_error
    assert [x["name"] for x in labels.data["result"]] == ["ember", "other"]


async def test_hr_reads_hr_only(mcp_client, seeded_db):
    res = await mcp_client.call(carol_hr, "get_doc", {"id": f"doc:{seeded_db['documents']['hr']}"})
    assert not res.is_error and res.data["audience"] == "hr"
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{seeded_db['documents']['hr']}"})
    assert res.is_error and res.text.endswith("not found")


async def test_claim_audiences_do_not_widen_a_person(mcp_client, seeded_db):
    """The claim lists ``hr``, but alice's email only resolves to ``all``: only ``all`` is returned."""
    assert alice_all.audiences == frozenset({"all"})
    args = {"query": "chunk", "min_tier": "T3", "limit": 50}
    token = mint_claim(
        alice_all.sub,
        "claude_client",
        ["all", "hr"],
        email=KH_CONFIG.people["alice"],
        tool="search",
        arguments=args,
    )
    res = await mcp_client.call(alice_all, "search", args, token=token)
    assert not res.is_error, res.text
    assert {h["audience"] for h in res.data["hits"]} == {"all"}
    assert f"doc:{seeded_db['documents']['hr']}" not in {h["id"] for h in res.data["hits"]}
