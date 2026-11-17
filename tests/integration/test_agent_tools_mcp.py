"""The five agent tools through the MCP client with both flags off (plan 16, 3.3)."""

import uuid

import pytest

from knowledge_hub.agent import texts
from knowledge_hub.mcp import errors
from tests.support.principals import alice_all

NOTE = {
    "audience": "all",
    "id": "a-note",
    "title": "A note",
    "domain": "technical",
    "review_by": "2999-01-01",
    "body": "body",
}
DOC = {
    "audience": "all",
    "domain": "technical",
    "product": "p",
    "kind": "guides",
    "id": "a-doc",
    "title": "A doc",
    "review_by": "2999-01-01",
    "body": "body",
    "rationale": "why",
}
EDIT = {
    "target_id": f"doc:{uuid.uuid4()}",
    "title": "T",
    "review_by": "2999-01-01",
    "body": "body",
    "rationale": "why",
}


@pytest.mark.parametrize(
    ("tool", "args", "text"),
    [
        ("write_note", NOTE, texts.DISABLED_NOTES),
        ("propose_doc", DOC, texts.DISABLED_PROPOSALS),
        ("propose_edit", EDIT, texts.DISABLED_PROPOSALS),
    ],
)
async def test_write_tools_are_disabled_by_default(mcp_client, tool, args, text):
    res = await mcp_client.call(alice_all, tool, args)
    assert res.status == 200 and res.is_error and res.text.endswith(text), res


async def test_pr_status_of_an_unknown_id_is_not_found(mcp_client):
    res = await mcp_client.call(alice_all, "pr_status", {"proposal_id": str(uuid.uuid4())})
    assert res.is_error and res.text.endswith(errors.NOT_FOUND), res


async def test_list_my_proposals_is_empty(mcp_client):
    res = await mcp_client.call(alice_all, "list_my_proposals", {})
    assert not res.is_error and res.data["proposals"] == [], res
