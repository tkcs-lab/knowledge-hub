"""``get_doc``: one RLS query per id kind; a missing and a hidden row are indistinguishable."""

import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer
from psycopg.rows import dict_row

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps

_KINDS = ("doc", "raw", "derived")

_DOC_SQL = """
SELECT d.id, d.repo, d.path, d.body AS text, d.tier, d.domain, d.owner, d.approved_by, d.approved_at,
       d.review_by, d.status, d.stale, d.entities, d.supersedes, s.id AS superseded_by,
       d.commit_sha, d.indexed_at
  FROM kh.documents d
  LEFT JOIN kh.documents s ON s.id = d.superseded_by
 WHERE d.id = %s
"""
_RAW_SQL = """
SELECT r.id, r.excerpt AS text, r.domain, r.entities, r.ingested_at AS indexed_at, r.source_url
  FROM kh.raw_records r WHERE r.id = %s
"""
_DERIVED_SQL = """
SELECT r.id, r.body AS text, r.domain, r.entities, r.created_at AS indexed_at
  FROM kh.derived_records r WHERE r.id = %s
"""


def parse_id(value: str) -> tuple[str, uuid.UUID] | None:
    """``kind:uuid`` in canonical lower-case form, or ``None`` when malformed."""
    kind, sep, raw = value.partition(":")
    if not sep or kind not in _KINDS:
        return None
    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        return None
    return (kind, parsed) if str(parsed) == raw else None


def source_link(deps: Deps, repo: str, commit_sha: str, path: str) -> str:
    base = deps.config.forgejo_web_url
    if not base:
        return ""
    return f"{base.rstrip('/')}/{deps.settings.forgejo_org}/{repo}/src/commit/{commit_sha}/{path}"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def _one(call: ToolCall, sql: str, key: uuid.UUID) -> dict[str, Any]:
    async with call.conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (key,))
        row = await cur.fetchone()
    if row is None:
        raise kh_error("not_found", errors.NOT_FOUND)
    return row


async def get_doc(call: ToolCall, id: str, version: str = "") -> dict[str, Any]:
    parsed = parse_id(id)
    if parsed is None:
        raise kh_error("not_found", errors.NOT_FOUND)
    if version:
        raise kh_error("version_not_indexed", errors.VERSION_NOT_INDEXED, sha=version)
    kind, key = parsed
    out: dict[str, Any]
    if kind == "doc":
        row = await _one(call, _DOC_SQL, key)
        out = {
            "text": row["text"],
            "tier": row["tier"],
            "domain": row["domain"],
            "owner": row["owner"] or "",
            "approved_by": list(row["approved_by"] or []),
            "approved_at": _iso(row["approved_at"]),
            "review_by": _iso(row["review_by"]),
            "status": row["status"],
            "stale": row["stale"],
            "entities": list(row["entities"] or []),
            "supersedes": list(row["supersedes"] or []),
            "superseded_by": f"doc:{row['superseded_by']}" if row["superseded_by"] else None,
            "commit_sha": row["commit_sha"],
            "indexed_at": _iso(row["indexed_at"]),
            "source_link": source_link(call.deps, row["repo"], row["commit_sha"], row["path"]),
        }
    else:
        row = await _one(call, _RAW_SQL if kind == "raw" else _DERIVED_SQL, key)
        out = {
            "text": row["text"],
            "tier": "T3" if kind == "raw" else "T2",
            "domain": row["domain"],
            "owner": "",
            "approved_by": [],
            "approved_at": None,
            "review_by": None,
            "status": "current",
            "stale": False,
            "entities": list(row["entities"] or []),
            "supersedes": [],
            "superseded_by": None,
            "commit_sha": "",
            "indexed_at": _iso(row["indexed_at"]),
            "source_link": (row["source_url"] or "") if kind == "raw" else "",
        }
    out["citation_id"] = f"{kind}:{key}"
    return out


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="get_doc",
        description=(
            "Return one document, raw record or derived record by citation id "
            "(doc:<uuid>, raw:<uuid> or derived:<uuid>) with its tier, owner, approvals and freshness."
        ),
    )(tool_guard("get_doc", deps)(get_doc))
