"""``flag_conflict`` and ``list_conflicts``: contradictions between records (plan 3.4, 3.8, 3.9)."""

import logging
import uuid
from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer
from psycopg.rows import dict_row

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools.docs import (
    MAX_LIST_LIMIT,
    cursor_key,
    decode_keyset,
    encode_keyset,
    keyset_scope,
    parse_id,
)

log = logging.getLogger("knowledge_hub.tools")

MAX_NOTE = 2000

# Local fixed texts (the catalogue has no entries for these argument errors).
SAME = "a and b must name two different records"
BAD_NOTE = "note must be 1 to 2000 characters"

_DOMAIN_SQL = {
    "doc": "SELECT domain FROM kh.documents WHERE id = %s",
    "raw": "SELECT domain FROM kh.raw_records WHERE id = %s",
    "derived": "SELECT domain FROM kh.derived_records WHERE id = %s",
}
_INSERT = """
INSERT INTO kh.conflicts (id, state, domain, note, created_by_sub, required_audiences, allowed_emails)
VALUES (%s, 'open', %s, %s, %s, %s, %s)
"""
_INSERT_ITEM = """
INSERT INTO kh.conflict_items (conflict_id, item_kind, item_id, src_required_audiences)
VALUES (%s, %s, %s, '{}')
"""


def _parse(value: str) -> tuple[str, uuid.UUID]:
    parsed = parse_id(value)
    if parsed is None:
        raise kh_error("not_found", errors.NOT_FOUND)
    return parsed


async def flag_conflict(call: ToolCall, a: str, b: str, note: str) -> dict[str, Any]:
    conn, caller = call.conn, call.caller
    (ka, ua), (kb, ub) = _parse(a), _parse(b)
    if (ka, ua) == (kb, ub):
        raise errors.KhError("same", SAME)
    if not 1 <= len(note) <= MAX_NOTE:
        raise errors.KhError("bad_note", BAD_NOTE)
    ids = [f"{ka}:{ua}", f"{kb}:{ub}"]
    cur = await conn.execute(
        "SELECT n_resolved, required_audiences, allowed_emails FROM kh.strict_visibility(%s::text[])", (ids,)
    )
    n_resolved, audiences, emails = await cur.fetchone()
    if n_resolved != 2:
        # Missing and hidden ids are indistinguishable.
        raise kh_error("not_found", errors.NOT_FOUND)
    cur = await conn.execute(_DOMAIN_SQL[ka], (ua,))
    row = await cur.fetchone()
    if row is None:
        raise kh_error("not_found", errors.NOT_FOUND)
    conflict_id = uuid.uuid4()
    await conn.execute(
        _INSERT,
        (
            conflict_id,
            row[0],
            note,
            caller.sub,
            list(audiences),
            None if emails is None else list(emails),
        ),
    )
    for kind, key in ((ka, ua), (kb, ub)):
        await conn.execute(_INSERT_ITEM, (conflict_id, kind, str(key)))
    try:
        # Run the deferred visibility check now so a failure is an INTERNAL error with a logged reason.
        await conn.execute("SET CONSTRAINTS kh.conflict_visibility_matches IMMEDIATE")
    except psycopg.Error as exc:
        ref = uuid.uuid4()
        log.error("conflict visibility check failed sqlstate=%s ref=%s: %s", exc.sqlstate, ref, exc)
        raise kh_error("internal", errors.INTERNAL, ref=ref) from None
    return {"conflict_id": str(conflict_id)}


_LIST_SQL = """
SELECT c.id, c.state, c.domain, c.note, c.created_at, c.closed_at, c.closed_by_pr,
       coalesce((SELECT array_agg(i.item_kind || ':' || i.item_id ORDER BY i.item_kind, i.item_id)
                   FROM kh.conflict_items i WHERE i.conflict_id = c.id), '{}') AS items
  FROM kh.conflicts c
 WHERE (%(domain)s = '' OR c.domain = %(domain)s)
   AND (%(status)s = '' OR c.state = %(status)s)
   AND (%(after_id)s::uuid IS NULL OR (c.created_at, c.id) < (%(after_at)s::timestamptz, %(after_id)s::uuid))
 ORDER BY c.created_at DESC, c.id DESC
 LIMIT %(n)s
"""


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def list_conflicts(
    call: ToolCall, domain: str = "", status: str = "", limit: int = 50, cursor: str = ""
) -> dict[str, Any]:
    limit = max(1, min(MAX_LIST_LIMIT, limit))
    caller, key = call.caller, cursor_key(call.deps)
    scope = keyset_scope("list_conflicts", domain, status)
    after_at: str | None = None
    after_id: str | None = None
    if cursor:
        after_at, after_id = decode_keyset(key, caller.sub, caller.audiences, scope, cursor, 2)
        try:
            uuid.UUID(str(after_id))
        except ValueError:
            raise errors.KhError("cursor_foreign", errors.CURSOR_FOREIGN) from None
    async with call.conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _LIST_SQL,
            {"domain": domain, "status": status, "after_id": after_id, "after_at": after_at, "n": limit + 1},
        )
        rows = await cur.fetchall()
    page = rows[:limit]
    next_cursor = ""
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_keyset(
            key, caller.sub, caller.audiences, scope, [last["created_at"].isoformat(), str(last["id"])]
        )
    return {
        "conflicts": [
            {
                "id": str(r["id"]),
                "state": r["state"],
                "domain": r["domain"] or "",
                "note": r["note"] or "",
                "created_at": _iso(r["created_at"]),
                "closed_at": _iso(r["closed_at"]),
                "closed_by_pr": r["closed_by_pr"],
                "items": list(r["items"]),
            }
            for r in page
        ],
        "next_cursor": next_cursor,
    }


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="flag_conflict",
        description=(
            "Record that two records (doc:, raw: or derived: ids) contradict each other, with a note. "
            "The conflict is visible exactly to those who may see both."
        ),
    )(tool_guard("flag_conflict", deps)(flag_conflict))
    server.tool(
        name="list_conflicts",
        description="List conflicts you may see, newest first; filter by domain and status (open or closed).",
    )(tool_guard("list_conflicts", deps)(list_conflicts))
