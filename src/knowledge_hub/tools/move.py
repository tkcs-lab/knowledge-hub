"""``move_item``: move a record, or a document, from one group the caller holds to another (plan 3.1, 3.2)."""

import logging
from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools.docs import parse_id

log = logging.getLogger("knowledge_hub.tools")

NOT_HELD_SQLSTATE = "P0K08"
WRONG_GROUP_SQLSTATE = "P0K09"
LINKED_SQLSTATE = "P0K0A"

DESCRIPTION = (
    "Move a record (rec:) or a document (doc:) from from_group to to_group, two groups you hold; "
    "from_group must be the group the item is in now. A record moves at once and keeps its id. "
    "A document moves with two pull requests: one adds it to to_group's repo, one removes it from "
    "from_group's repo; each group's approvers merge their own, add first. The document gets a new "
    "doc: id when the target repo is indexed. This is not a purge: the old repo's history, past usage "
    "queries and backups keep the old copy. Claude clients only."
)


async def _move_record(call: ToolCall, key: str, from_group: str, to_group: str) -> dict[str, Any]:
    try:
        async with call.conn.transaction():  # a savepoint: the function's refusal must not abort the call
            async with call.conn.cursor() as cur:
                await cur.execute("SELECT kh.move_record(%s, %s, %s)", (key, from_group, to_group))
                row = await cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise kh_error("move_conflict", errors.MOVE_CONFLICT) from None
    except psycopg.errors.ForeignKeyViolation:
        raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD) from None
    except psycopg.errors.Error as exc:
        if exc.sqlstate == NOT_HELD_SQLSTATE:
            raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD) from None
        if exc.sqlstate == WRONG_GROUP_SQLSTATE:
            raise kh_error("move_wrong_group", errors.MOVE_WRONG_GROUP) from None
        if exc.sqlstate == LINKED_SQLSTATE:
            raise kh_error("move_linked", errors.MOVE_LINKED) from None
        raise
    if row is None or row[0] is None:
        raise kh_error("not_found", errors.NOT_FOUND)
    rid = f"rec:{key}"
    log.info("move_item sub=%s id=%s from=%s to=%s", call.caller.sub, rid, from_group, to_group)
    return {"tier": "T3", "id": rid, "from_group": from_group, "to_group": to_group, "moved": True}


async def move_item(call: ToolCall, id: str, from_group: str, to_group: str) -> dict[str, Any]:
    caller, deps = call.caller, call.deps
    if caller.principal_type != "claude_client":
        raise kh_error("not_for_workflows", errors.NOT_FOR_WORKFLOWS)
    if from_group == to_group:
        raise kh_error("bad_arg", errors.BAD_ARG, name="to_group")
    for group in (from_group, to_group):
        if group not in caller.audiences or not deps.config.is_active(group):
            raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD)
    parsed = parse_id(id)
    if parsed is None:
        raise kh_error("bad_id", errors.BAD_ID)
    if parsed[0] == "rec":
        return await _move_record(call, str(parsed[1]), from_group, to_group)
    # Phase p2 replaces this branch with the document move.
    raise kh_error("bad_arg", errors.BAD_ARG, name="id")


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(name="move_item", description=DESCRIPTION)(tool_guard("move_item", deps)(move_item))
