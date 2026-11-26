"""``delete_records``: hide the records a source no longer has (plan 3.3)."""

import logging
from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps

log = logging.getLogger("knowledge_hub.tools")

MAX_REFS = 500
CAP_SQLSTATE = "P0K0E"
SHRINK_SQLSTATE = "P0K10"
DATASET_SQLSTATE = "P0K0B"


def _bad(name: str):
    return kh_error("bad_arg", errors.BAD_ARG, name=name)


def _ref_ok(ref: Any, prefix: bool) -> bool:
    if not isinstance(ref, str) or "\x00" in ref:
        return False
    return (8 if prefix else 1) <= len(ref) <= 200


async def delete_records(
    call: ToolCall, audience: str, source: str, source_refs: list[str], prefix: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    if call.caller.principal_type != "n8n_workflow":
        raise kh_error("only_workflows", errors.ONLY_WORKFLOWS)
    if audience not in call.caller.audiences or not call.deps.config.is_active(audience):
        raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD)
    if not source or len(source) > 200 or "\x00" in source or source.startswith("dataset:"):
        raise _bad("source")
    if not source_refs or not all(_ref_ok(r, prefix) for r in source_refs):
        raise _bad("source_refs")
    if len(source_refs) > MAX_REFS:
        raise kh_error("too_many_rows", errors.TOO_MANY_ROWS)
    try:
        async with call.conn.transaction():  # a savepoint: the function's refusal must not abort the call
            async with call.conn.cursor() as cur:
                await cur.execute(
                    "SELECT kh.delete_records(%s, %s, %s, %s, %s)",
                    (audience, source, source_refs, prefix, force),
                )
                row = await cur.fetchone()
    except psycopg.errors.Error as exc:
        if exc.sqlstate == CAP_SQLSTATE:
            raise kh_error("records_cap", errors.RECORDS_CAP) from None
        if exc.sqlstate == SHRINK_SQLSTATE:
            n, limit = (exc.diag.message_primary or "").split()[1:3]
            raise kh_error("records_shrink", errors.RECORDS_SHRINK, n=n, limit=limit) from None
        if exc.sqlstate == DATASET_SQLSTATE:
            raise _bad("source") from None
        raise
    if row is None or row[0] is None:
        raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD)
    n = int(row[0])
    log.info(
        "delete_records sub=%s audience=%s source=%s refs=%d prefix=%s outdated=%d",
        call.caller.sub, audience, source, len(source_refs), prefix, n,
    )
    return {"outdated": n}


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="delete_records",
        description=(
            "Hide the records of a source that the source no longer has (hub keys only): by exact "
            "source_ref, or with prefix=true by the start of source_ref. Hidden records leave search "
            "and are deleted for good after the purge period. Sending a source_ref again with "
            "ingest_record restores it. At most 500 refs per call; more than a fifth of a source "
            "within 24 hours needs force=true."
        ),
    )(tool_guard("delete_records", deps)(delete_records))
