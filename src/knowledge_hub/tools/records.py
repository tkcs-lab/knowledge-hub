"""``ingest_record``: store one raw record (tier T3) readable by the named group (plan 3.5, 3.8)."""

import uuid
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from psycopg import errors as pg_errors

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.search.similar import similar_items

EXCERPT_CHARS = 2000

_EXISTING_SQL = "SELECT id FROM kh.records WHERE audience = %s AND source = %s AND source_ref = %s"
_INSERT_SQL = """
INSERT INTO kh.records (id, audience, source, source_ref, source_url, title, blob_key, excerpt,
                        content_type, occurred_at, labels, written_by, created_by_sub, created_by_type)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


async def _existing(call: ToolCall, audience: str, source: str, source_ref: str) -> str | None:
    async with call.conn.cursor() as cur:
        await cur.execute(_EXISTING_SQL, (audience, source, source_ref))
        row = await cur.fetchone()
    return f"rec:{row[0]}" if row else None


def _nul_arg(texts: dict[str, str], labels: list[str]) -> str | None:
    """The name of the first argument that holds a NUL character, which Postgres text cannot store."""
    for name, value in texts.items():
        if "\x00" in value:
            return name
    return "labels" if any("\x00" in label for label in labels) else None


async def ingest_record(
    call: ToolCall,
    source: str,
    source_ref: str,
    title: str,
    body: str,
    audience: str,
    occurred_at: str,
    labels: list[str] = [],  # noqa: B006 - the guard reads the default; it is never mutated
    source_url: str = "",
    content_type: str = "text/plain",
) -> dict[str, Any]:
    if call.caller.principal_type != "n8n_workflow":
        raise kh_error("only_workflows", errors.ONLY_WORKFLOWS)
    deps, caller = call.deps, call.caller
    if audience not in caller.audiences or not deps.config.is_active(audience):
        raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD)
    bad = _nul_arg(
        {"source": source, "source_ref": source_ref, "title": title, "body": body,
         "source_url": source_url, "content_type": content_type},
        labels,
    )
    if bad is not None:
        raise kh_error("bad_arg", errors.BAD_ARG, name=bad)
    if source.startswith("dataset:"):
        raise kh_error("bad_arg", errors.BAD_ARG, name="source")
    data = body.encode()
    if len(data) > deps.settings.max_record_bytes:
        raise kh_error("too_large", errors.TOO_LARGE)
    try:
        when = datetime.fromisoformat(occurred_at)
    except ValueError:
        raise kh_error("bad_arg", errors.BAD_ARG, name="occurred_at") from None
    if when.tzinfo is None or when.utcoffset() is None:
        raise kh_error("bad_arg", errors.BAD_ARG, name="occurred_at")
    wanted = sorted(set(labels))
    if wanted:
        async with call.conn.cursor() as cur:
            await cur.execute("SELECT name FROM kh.labels WHERE name = ANY(%s)", (wanted,))
            known = {r[0] for r in await cur.fetchall()}
        if known != set(wanted) or set(wanted) & deps.config.archived_label_set:
            raise kh_error("bad_label", errors.BAD_LABEL)
    found = await _existing(call, audience, source, source_ref)
    if found is not None:
        return {"id": found, "existed": True}
    async with call.conn.cursor() as cur:
        await cur.execute("SELECT kh.record_moved(%s, %s, %s)", (audience, source, source_ref))
        moved = await cur.fetchone()
    if moved is not None and moved[0]:
        raise kh_error("record_moved", errors.RECORD_MOVED)

    key = uuid.uuid4()
    blob_key = f"records/{key}"
    await deps.blobs.put(blob_key, data, content_type)
    try:
        async with call.conn.transaction():  # a savepoint: a unique violation must not abort the call
            await call.conn.execute(
                _INSERT_SQL,
                (key, audience, source, source_ref, source_url, title, blob_key, body[:EXCERPT_CHARS],
                 content_type, when, list(labels), deps.config.bot_login, caller.sub,
                 caller.principal_type),
            )
    except pg_errors.UniqueViolation:
        await deps.blobs.delete(blob_key)
        found = await _existing(call, audience, source, source_ref)
        if found is None:
            raise
        return {"id": found, "existed": True}
    except pg_errors.Error:
        await deps.blobs.delete(blob_key)
        raise
    similar = await similar_items(call, f"{title}\n\n{body[:EXCERPT_CHARS]}", audience)
    return {"id": f"rec:{key}", "existed": False, "similar": similar}


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="ingest_record",
        description=(
            "Store a raw record (a meeting note, message or similar) readable by the named audience "
            "(hub keys only; the audience must be the key's group). "
            "Re-sending the same source and source_ref returns the existing id. "
            "occurred_at is ISO 8601 with a timezone. "
            "The reply of a new record lists up to three similar items of the same group in similar."
        ),
    )(tool_guard("ingest_record", deps)(ingest_record))
