"""``ingest_raw``: store one T3 record; its visibility is assigned by the database (plan 3.7, 3.9)."""

import uuid
from datetime import datetime
from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer
from psycopg.types.json import Jsonb

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps

EXCERPT_CHARS = 2000

_INSERT = """
INSERT INTO kh.raw_records (id, source_system, source_ref, source_url, series, folder, participants,
    title, blob_key, excerpt, content_type, occurred_at, ingested_by_sub, ingested_by_type, domain,
    entities, generated_by, required_audiences, allowed_emails)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}', NULL)
"""


def _occurred_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise errors.KhError("bad_occurred_at", "occurred_at must be ISO 8601 with a UTC offset")
    return parsed


async def ingest_raw(
    call: ToolCall,
    source: str,
    source_ref: str,
    title: str,
    body: str,
    domain: str,
    occurred_at: str,
    series_id: str = "",
    folder_id: str = "",
    participants: list[str] = [],  # noqa: B006 - never mutated
    entities: list[str] = [],  # noqa: B006
    source_url: str = "",
    content_type: str = "text/plain",
    generated_by: str = "",
) -> dict[str, Any]:
    deps, caller = call.deps, call.caller
    if caller.principal_type not in deps.config.ingest_principal_types:
        raise kh_error("ingest_forbidden", errors.INGEST_FORBIDDEN)
    data = body.encode("utf-8")
    if len(data) > deps.settings.max_raw_bytes:
        raise kh_error("ingest_too_large", errors.INGEST_TOO_LARGE, n=deps.settings.max_raw_bytes)
    if domain not in deps.config.domains:
        raise errors.KhError("bad_domain", "domain must be one of " + ", ".join(deps.config.domains))
    when = _occurred_at(occurred_at)
    if deps.blobs is None:
        raise RuntimeError("no blob store configured")

    raw_id = uuid.uuid4()
    blob_key = f"raw/{raw_id}"
    params = (
        raw_id,
        source,
        source_ref,
        source_url or None,
        series_id or None,
        folder_id or None,
        list(participants),
        title,
        blob_key,
        body[:EXCERPT_CHARS],
        content_type,
        when,
        caller.sub,
        caller.principal_type,
        domain,
        list(entities),
        Jsonb({"label": generated_by}) if generated_by else None,
    )
    try:
        async with call.conn.transaction():  # a savepoint: a failed insert leaves the outer tx usable
            await call.conn.execute(_INSERT, params)
    except psycopg.errors.UniqueViolation:
        cur = await call.conn.execute("SELECT kh.my_raw_id(%s, %s)", (source, source_ref))
        existing = (await cur.fetchone() or [None])[0]
        if existing is None:
            raise
        raise kh_error("already_ingested", errors.ALREADY_INGESTED, id=f"raw:{existing}") from None
    except psycopg.Error as exc:
        if exc.sqlstate == "P0K01":
            raise kh_error("ingest_no_map", errors.INGEST_NO_MAP) from None
        if exc.sqlstate == "P0K02":
            raise kh_error("ingest_audience", errors.INGEST_AUDIENCE) from None
        raise
    # Before commit: a failed upload raises, the transaction rolls back and no row remains.
    await deps.blobs.put(blob_key, data, content_type)
    return {"citation_id": f"raw:{raw_id}"}


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="ingest_raw",
        description=(
            "Store one raw (T3) record such as a meeting transcript. Workflow principals only. "
            "Visibility comes from the configured series/folder audience map, else from participants."
        ),
    )(tool_guard("ingest_raw", deps)(ingest_raw))
