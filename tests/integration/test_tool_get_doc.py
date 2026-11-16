import re
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_hub.auth.claims import Call
from knowledge_hub.mcp.errors import tool_guard
from knowledge_hub.tools import docs
from knowledge_hub.visibility import Visibility, can_read
from tests.support.claims import mint_claim
from tests.support.pg import FRESH_DB
from tests.support.principals import PRINCIPALS, alice_all, bob_mgmt
from tests.support.seed import CASES

NOT_FOUND = "not found"


def _visible(caller, case: str) -> bool:
    return can_read(Visibility(*CASES[case]), caller.audiences, caller.email)


KINDS = [("documents", "doc"), ("raw_records", "raw"), ("derived_records", "derived")]


@pytest.mark.parametrize("principal", sorted(PRINCIPALS))
@pytest.mark.parametrize("table,prefix", KINDS)
async def test_each_principal_gets_exactly_its_rows(seeded_db, mcp_client, principal, table, prefix):
    caller = PRINCIPALS[principal]
    for case, row_id in seeded_db[table].items():
        res = await mcp_client.call(caller, "get_doc", {"id": f"{prefix}:{row_id}"})
        if _visible(caller, case):
            assert not res.is_error, (principal, table, case, res.text)
            assert res.data["citation_id"] == f"{prefix}:{row_id}"
            assert res.data["tier"] == {"doc": "T0", "raw": "T3", "derived": "T2"}[prefix]
        else:
            assert res.is_error and res.text.endswith(NOT_FOUND), (principal, table, case, res.text)


async def test_document_fields(seeded_db, mcp_client):
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{seeded_db['documents']['all']}"})
    assert res.data["text"] == "Body all"
    assert res.data["owner"] == "team"
    assert res.data["approved_by"] == ["x"]
    assert res.data["stamp"] == "reviewed"
    for table, prefix in (("raw_records", "raw"), ("derived_records", "derived")):
        other = await mcp_client.call(alice_all, "get_doc", {"id": f"{prefix}:{seeded_db[table]['all']}"})
        assert other.data["stamp"] == ""
    assert res.data["commit_sha"] == "c1"
    assert res.data["superseded_by"] is None
    assert res.data["entities"] == ["prd:a"]


async def test_agent_written_flag(seeded_db, fresh_db, mcp_client):
    doc_id = seeded_db["documents"]["all"]
    raw_id = seeded_db["raw_records"]["all"]
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{doc_id}"})
    assert res.data["agent_written"] is False
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        conn.execute("UPDATE kh.documents SET agent_written = true WHERE id = %s", (doc_id,))
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{doc_id}"})
    assert res.data["agent_written"] is True
    raw = await mcp_client.call(alice_all, "get_doc", {"id": f"raw:{raw_id}"})
    assert raw.data["agent_written"] is False


async def test_successor_is_shown_only_if_visible(seeded_db, fresh_db, mcp_client):
    old, new = seeded_db["documents"]["all"], seeded_db["documents"]["mgmt"]
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        conn.execute("UPDATE kh.documents SET superseded_by = %s WHERE id = %s", (new, old))
    hidden = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{old}"})
    shown = await mcp_client.call(bob_mgmt, "get_doc", {"id": f"doc:{old}"})
    assert hidden.data["superseded_by"] is None
    assert shown.data["superseded_by"] == f"doc:{new}"


async def test_missing_hidden_and_malformed_ids_are_identical(seeded_db, mcp_client):
    hidden = seeded_db["documents"]["mgmt"]
    ids = [
        f"doc:{uuid.uuid4()}",
        f"raw:{uuid.uuid4()}",
        f"derived:{uuid.uuid4()}",
        f"doc:{hidden}",
        f"raw:{seeded_db['raw_records']['mgmt']}",
        f"derived:{seeded_db['derived_records']['mgmt']}",
        "doc:not-a-uuid",
        "nonsense",
        "",
        f"foo:{uuid.uuid4()}",
        f"doc:{str(hidden).upper()}",
    ]
    calls = [await mcp_client.call(alice_all, "get_doc", {"id": i}) for i in ids]
    results = {(r.status, r.is_error, r.text) for r in calls}
    assert len(results) == 1
    (status, is_error, text), = results
    assert status == 200 and is_error and text.endswith(NOT_FOUND)


async def test_version_is_not_indexed_for_now(seeded_db, mcp_client):
    res = await mcp_client.call(
        alice_all, "get_doc", {"id": f"doc:{seeded_db['documents']['all']}", "version": "abc123"}
    )
    assert res.is_error and "version abc123 is not an indexed version of this document" in res.text


async def test_database_error_is_an_opaque_internal_error(seeded_db, mcp_client, monkeypatch):
    # Postgres puts the offending row value into this error's message; it must not reach the caller.
    monkeypatch.setattr(docs, "_DOC_SQL", "SELECT (SELECT body::int FROM kh.documents WHERE id = %s) AS x")
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{seeded_db['documents']['all']}"})
    assert res.is_error
    assert re.search(r"internal error \(ref [0-9a-f-]{36}\)$", res.text)
    assert "Body all" not in res.text and "integer" not in res.text


async def test_defaults_filled_in_by_the_sdk_are_accepted(seeded_db, mcp_client):
    # The claim is bound to {"id": ...}; the SDK dispatches id plus the default version="".
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{seeded_db['documents']['all']}"})
    assert not res.is_error


async def test_claim_for_other_arguments_is_refused(seeded_db, mcp_client):
    row = seeded_db["documents"]["all"]
    token = mint_claim(
        alice_all.sub, "claude_client", ["all"], alice_all.email, "get_doc", {"id": f"doc:{uuid.uuid4()}"}
    )
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{row}"}, token=token)
    assert res.status == 401


def _fake_ctx(state):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(scope={"state": state})))


async def test_guard_refuses_dispatch_that_differs_from_stored_arguments(deps):
    async def probe(call, a: int = 1, b: str = "x"):
        return {"a": a, "b": b}

    wrapped = tool_guard("probe", deps)(probe)

    def state(name="probe", **arguments):
        return {"kh_caller": alice_all, "kh_call": Call(name, arguments)}

    # Stored arguments overlaid on the declared defaults are exactly what the SDK passes.
    assert await wrapped(_fake_ctx(state(a=2)), a=2, b="x") == {"a": 2, "b": "x"}
    assert await wrapped(_fake_ctx(state()), a=1, b="x") == {"a": 1, "b": "x"}
    assert await wrapped(_fake_ctx(state(a=2)), a=2.0, b="x") == {"a": 2.0, "b": "x"}
    for ctx, kwargs in [
        (_fake_ctx(state(a=2)), {"a": 3, "b": "x"}),  # value differs
        (_fake_ctx(state(a=2)), {"a": 2, "b": "y"}),  # a default was changed
        (_fake_ctx(state(a=2)), {"a": 2}),  # an argument went missing
        (_fake_ctx(state(a=2)), {"a": 2, "b": "x", "c": 1}),  # an extra argument
        (_fake_ctx(state(name="other", a=2)), {"a": 2, "b": "x"}),  # another tool's call
        (_fake_ctx({"kh_caller": alice_all, "kh_call": None}), {"a": 1, "b": "x"}),  # not a tools/call
        (_fake_ctx({"kh_call": Call("probe", {})}), {"a": 1, "b": "x"}),  # no caller
        (_fake_ctx({}), {"a": 1, "b": "x"}),
    ]:
        with pytest.raises(ToolError, match=r"^internal error \(ref [0-9a-f-]{36}\)$"):
            await wrapped(ctx, **kwargs)
