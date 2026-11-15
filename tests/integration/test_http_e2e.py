"""The app over HTTP with signed claims, as PrivacyFence calls it (plan 3.2)."""

import json

from knowledge_hub.auth.pf_claim_verify import CLAIM_HEADER
from tests.integration.test_search_ranking import app_settings, deps  # noqa: F401  (fixtures)
from tests.support.claims import mint_claim, rpc_body
from tests.support.mcp_client import PROTOCOL_VERSION
from tests.support.principals import alice_all, dave_none, wf_token

BANNED_SCHEMA_KEYS = {"$ref", "$defs", "definitions"}


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _claim(caller, **kwargs):
    return mint_claim(caller.sub, caller.principal_type, email=caller.email, **kwargs)


async def test_initialize_then_tools_list_has_flat_schemas(mcp_client):
    init = rpc_body(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pf", "version": "0"},
        },
        id=0,
    )
    first = await mcp_client.post(init, _claim(alice_all, audiences=[]))
    assert first.status_code == 200 and first.json()["result"]["serverInfo"]
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    assert (await mcp_client.post(note, _claim(alice_all, audiences=[]))).status_code in (200, 202)
    listed = await mcp_client.post(rpc_body("tools/list", {}, id=2), _claim(alice_all, audiences=[]))
    assert listed.status_code == 200
    tools = listed.json()["result"]["tools"]
    assert {"search", "get_doc"} <= {t["name"] for t in tools}
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert len(json.dumps(schema)) < 32 * 1024
        for node in _walk(schema):
            assert not BANNED_SCHEMA_KEYS & node.keys(), tool["name"]
        assert "reason" not in schema.get("properties", {}), tool["name"]


async def test_host_outside_the_allowed_list_is_refused(mcp_client):
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        CLAIM_HEADER: _claim(alice_all, audiences=[]),
        "host": "evil.example:8080",
    }
    resp = await mcp_client.http.post("/mcp", content=rpc_body("tools/list", {}), headers=headers)
    assert resp.status_code in (400, 421), resp.status_code
    assert "result" not in resp.text


async def test_request_without_a_claim_is_401_with_an_empty_body(mcp_client):
    resp = await mcp_client.post(rpc_body("tools/list", {}), None)
    assert resp.status_code == 401 and resp.content == b""


async def test_claim_made_for_other_arguments_is_401(seeded_db, mcp_client):
    row = seeded_db["documents"]["all"]
    token = _claim(alice_all, audiences=["all"], tool="get_doc", arguments={"id": "doc:other"})
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{row}"}, token=token)
    assert res.status == 401 and res.text == ""


async def test_tools_call_with_a_service_claim_is_401(seeded_db, mcp_client):
    args = {"id": f"doc:{seeded_db['documents']['all']}"}
    token = mint_claim("svc", "service", audiences=["all"], tool="get_doc", arguments=args)
    res = await mcp_client.call(alice_all, "get_doc", args, token=token)
    assert res.status == 401 and res.text == ""


async def test_workflow_claim_never_sees_an_attendee_only_row(seeded_db, mcp_client):
    row = seeded_db["documents"]["attendee"]
    res = await mcp_client.call(wf_token, "get_doc", {"id": f"doc:{row}"})
    assert res.is_error and res.text.endswith("not found")
    # even when the claim carries the matching address, it is dropped for a workflow principal
    token = mint_claim(
        wf_token.sub, "n8n_workflow", ["all"], "dave@example.org", "get_doc", {"id": f"doc:{row}"}
    )
    res = await mcp_client.call(wf_token, "get_doc", {"id": f"doc:{row}"}, token=token)
    assert res.is_error and res.text.endswith("not found")


async def test_person_without_groups_sees_the_attendee_row_for_their_email(seeded_db, mcp_client):
    row = seeded_db["documents"]["attendee"]
    res = await mcp_client.call(dave_none, "get_doc", {"id": f"doc:{row}"})
    assert not res.is_error and res.data["citation_id"] == f"doc:{row}"


async def test_the_same_jti_twice_is_401_the_second_time(mcp_client):
    token = _claim(alice_all, audiences=[], jti="replayed-jti-1")
    body = rpc_body("tools/list", {}, id=3)
    assert (await mcp_client.post(body, token)).status_code == 200
    again = await mcp_client.post(body, token)
    assert again.status_code == 401 and again.content == b""
