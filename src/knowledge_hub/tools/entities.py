"""``get_entity`` and ``list_entities``: the Grist entity cache under RLS (plan 3.9, 3.12)."""

from typing import Any

from mcp.server.mcpserver import MCPServer
from psycopg.rows import dict_row

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools.docs import MAX_LIST_LIMIT, cursor_key, decode_keyset, encode_keyset, keyset_scope

ENTITY_TYPES = ("prd", "cmp", "ver", "cus")

# Local fixed text (the catalogue has no entry for this argument error).
BAD_TYPE = "type must be one of prd, cmp, ver, cus"

_COLUMNS = "id, kind, name, owner_team, status, attrs, synced_at"
_ENTITY_SQL = f"SELECT {_COLUMNS} FROM kh.entities_cache WHERE id = %s"
_DOCS_SQL = """
SELECT id, title, domain, tier, status FROM kh.documents WHERE %s = ANY(entities) ORDER BY id
"""
_LIST_SQL = f"""
SELECT {_COLUMNS}
  FROM kh.entities_cache
 WHERE kind = %(kind)s
   AND (%(status)s = '' OR status = %(status)s)
   AND (%(owner_team)s = '' OR owner_team = %(owner_team)s)
   AND (%(after_id)s::text IS NULL OR id > %(after_id)s::text)
 ORDER BY id ASC
 LIMIT %(n)s
"""


def _entity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"] or "",
        "name": row["name"] or "",
        "owner_team": row["owner_team"] or "",
        "status": row["status"] or "",
        "attrs": row["attrs"] or {},
        "synced_at": row["synced_at"].isoformat() if row["synced_at"] is not None else None,
    }


async def get_entity(call: ToolCall, id: str) -> dict[str, Any]:
    async with call.conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_ENTITY_SQL, (id,))
        row = await cur.fetchone()
        if row is None:  # missing and hidden are the same answer
            raise kh_error("not_found", errors.NOT_FOUND)
        await cur.execute(_DOCS_SQL, (id,))
        docs = await cur.fetchall()
    return {
        "entity": _entity(row),
        "documents": [
            {
                "citation_id": f"doc:{d['id']}",
                "title": d["title"],
                "domain": d["domain"],
                "tier": d["tier"],
                "status": d["status"],
            }
            for d in docs
        ],
    }


async def list_entities(
    call: ToolCall, type: str, status: str = "", owner_team: str = "", limit: int = 50, cursor: str = ""
) -> dict[str, Any]:
    if type not in ENTITY_TYPES:
        raise errors.KhError("bad_type", BAD_TYPE)
    limit = max(1, min(MAX_LIST_LIMIT, limit))
    caller, key = call.caller, cursor_key(call.deps)
    scope = keyset_scope("list_entities", type, status, owner_team)
    after_id: str | None = None
    if cursor:
        (after_id,) = decode_keyset(key, caller.sub, caller.audiences, scope, cursor, 1)
        if not isinstance(after_id, str):
            raise errors.KhError("cursor_foreign", errors.CURSOR_FOREIGN)
    async with call.conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _LIST_SQL,
            {"kind": type, "status": status, "owner_team": owner_team, "after_id": after_id, "n": limit + 1},
        )
        rows = await cur.fetchall()
    page = rows[:limit]
    next_cursor = ""
    if len(rows) > limit:
        next_cursor = encode_keyset(key, caller.sub, caller.audiences, scope, [page[-1]["id"]])
    return {"entities": [_entity(r) for r in page], "next_cursor": next_cursor}


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="get_entity",
        description=(
            "Return one entity (prd:, cmp:, ver: or cus: id) with the documents you may see that list it."
        ),
    )(tool_guard("get_entity", deps)(get_entity))
    server.tool(
        name="list_entities",
        description=(
            "List entities you may see of one type (prd, cmp, ver or cus), by id; "
            "filter by status and owner team."
        ),
    )(tool_guard("list_entities", deps)(list_entities))
