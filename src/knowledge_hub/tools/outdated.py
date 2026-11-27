"""``mark_record_outdated``: hide a raw record from search, or make it current again (plan 3.4)."""

import logging
from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools.docs import parse_id

log = logging.getLogger("knowledge_hub.tools")

MAX_NOTE_CHARS = 500
BAD_SUCCESSOR_SQLSTATE = "P0K07"
DATASET_SQLSTATE = "P0K0B"
PURGING_SQLSTATE = "P0K0F"


def _bad(name: str):
    return kh_error("bad_arg", errors.BAD_ARG, name=name)


async def mark_record_outdated(
    call: ToolCall, id: str, outdated: bool = True, replaced_by: str = "", note: str = ""
) -> dict[str, Any]:
    parsed = parse_id(id)
    if parsed is None or parsed[0] != "rec":
        raise _bad("id")
    if len(note) > MAX_NOTE_CHARS or "\x00" in note or (not outdated and note):
        raise _bad("note")
    if "\x00" in replaced_by or (not outdated and replaced_by):
        raise _bad("replaced_by")
    if replaced_by and parse_id(replaced_by) is None:
        raise _bad("replaced_by")
    try:
        async with call.conn.transaction():  # a savepoint: the function's refusal must not abort the call
            async with call.conn.cursor() as cur:
                await cur.execute(
                    "SELECT kh.mark_record_outdated(%s, %s, %s, %s)",
                    (parsed[1], outdated, replaced_by, note),
                )
                row = await cur.fetchone()
    except psycopg.errors.Error as exc:
        if exc.sqlstate == BAD_SUCCESSOR_SQLSTATE:
            raise _bad("replaced_by") from None
        if exc.sqlstate == DATASET_SQLSTATE:
            raise kh_error("dataset_record", errors.DATASET_RECORD) from None
        if exc.sqlstate == PURGING_SQLSTATE:
            raise kh_error("record_purging", errors.RECORD_PURGING) from None
        raise
    if row is None or row[0] is None:
        raise kh_error("not_found", errors.NOT_FOUND)
    log.info("mark_record_outdated sub=%s id=%s outdated=%s", call.caller.sub, id, outdated)
    return {"id": id, "outdated": outdated, "replaced_by": replaced_by if outdated else ""}


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="mark_record_outdated",
        description=(
            "Mark a raw record of a group you hold as outdated (hidden from search unless "
            "include_outdated is true), optionally naming the document or record that replaces it, "
            "or mark it current again with outdated=false."
        ),
    )(tool_guard("mark_record_outdated", deps)(mark_record_outdated))
