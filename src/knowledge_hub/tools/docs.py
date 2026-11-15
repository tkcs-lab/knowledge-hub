"""``get_doc``: one RLS query per id kind; a missing and a hidden row are indistinguishable."""

import base64
import datetime
import hashlib
import hmac
import json
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from psycopg.rows import dict_row

from knowledge_hub.blobs import BlobNotFound
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps

_KINDS = ("doc", "raw", "derived")
MAX_LIST_LIMIT = 50

_DOC_SQL = """
SELECT d.id, d.repo, d.path, d.body AS text, d.tier, d.domain, d.owner, d.approved_by, d.approved_at,
       d.review_by, d.status, d.stale, d.entities, d.supersedes, s.id AS superseded_by,
       d.commit_sha, d.indexed_at
  FROM kh.documents d
  LEFT JOIN kh.documents s ON s.id = d.superseded_by
 WHERE d.id = %s
"""
_RAW_SQL = """
SELECT r.id, r.excerpt AS text, r.blob_key, r.domain, r.entities, r.ingested_at AS indexed_at, r.source_url
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


async def _raw_text(call: ToolCall, row: dict[str, Any]) -> str:
    """The full body from the blob store; the excerpt when there is no store or no blob."""
    blobs = call.deps.blobs
    if blobs is None or not row["blob_key"]:
        return row["text"]
    try:
        return (await blobs.get(row["blob_key"])).decode("utf-8", errors="replace")
    except BlobNotFound:
        return row["text"]


async def get_doc(call: ToolCall, id: str, version: str = "") -> dict[str, Any]:
    parsed = parse_id(id)
    if parsed is None:
        raise kh_error("not_found", errors.NOT_FOUND)
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
        if kind == "raw":
            row["text"] = await _raw_text(call, row)
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
    if version:
        out = await _at_version(call, kind, key, version, out, row)
    out["citation_id"] = f"{kind}:{key}"
    return out


_VERSION_SQL = """
SELECT v.path, v.tier, v.status, v.approved_by, v.approved_at
  FROM kh.document_versions v WHERE v.document_id = %s AND v.commit_sha = %s
"""


async def _at_version(
    call: ToolCall, kind: str, key: uuid.UUID, version: str, out: dict[str, Any], row: dict[str, Any]
) -> dict[str, Any]:
    """The document as of an indexed commit; the visible current row has already been read (RLS)."""
    if kind != "doc":
        raise kh_error("version_not_indexed", errors.VERSION_NOT_INDEXED, sha=version)
    async with call.conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_VERSION_SQL, (key, version))
        ver = await cur.fetchone()
    if ver is None:
        raise kh_error("version_not_indexed", errors.VERSION_NOT_INDEXED, sha=version)
    text = await call.deps.forgejo.get_raw(row["repo"], ver["path"], version)
    out.update(
        text=text,
        tier=ver["tier"],
        status=ver["status"],
        approved_by=list(ver["approved_by"] or []),
        approved_at=_iso(ver["approved_at"]),
        commit_sha=version,
        source_link=source_link(call.deps, row["repo"], version, ver["path"]),
    )
    return out


def cursor_key(deps: Deps) -> bytes:
    path = deps.settings.cursor_key_file
    if path is None:
        raise RuntimeError("KH_CURSOR_KEY_FILE is not set")
    return Path(path).read_bytes().strip()


def keyset_scope(tool: str, *filters: str) -> str:
    """What a keyset cursor is bound to: the tool and every filter that shapes its list."""
    return hashlib.sha256("\x1f".join((tool, *filters)).encode()).hexdigest()


def _keyset_mac(key: bytes, sub: str, audiences: Iterable[str], scope: str, payload: bytes) -> bytes:
    aud = hashlib.sha256("\n".join(sorted(set(audiences))).encode()).hexdigest()
    msg = b"|".join((sub.encode(), aud.encode(), scope.encode(), payload))
    return hmac.new(key, msg, hashlib.sha256).digest()


def encode_keyset(
    key: bytes, sub: str, audiences: Iterable[str], scope: str, position: list[str | None]
) -> str:
    """``base64url(payload).base64url(HMAC(sub|audience-hash|scope|payload))`` for the last row's key."""
    payload = json.dumps(position).encode()
    mac = _keyset_mac(key, sub, audiences, scope, payload)
    enc = base64.urlsafe_b64encode
    return enc(payload).decode().rstrip("=") + "." + enc(mac).decode().rstrip("=")


def decode_keyset(
    key: bytes, sub: str, audiences: Iterable[str], scope: str, token: str, width: int
) -> list[str | None]:
    """The position a keyset cursor carries; a tampered or foreign cursor is ``CURSOR_FOREIGN``."""
    foreign = errors.KhError("cursor_foreign", errors.CURSOR_FOREIGN)
    head, sep, tail = token.partition(".")
    if not sep:
        raise foreign
    try:
        payload = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
        mac = base64.urlsafe_b64decode(tail + "=" * (-len(tail) % 4))
        position = json.loads(payload)
    except ValueError:
        raise foreign from None
    if not hmac.compare_digest(mac, _keyset_mac(key, sub, audiences, scope, payload)):
        raise foreign
    if not isinstance(position, list) or len(position) != width:
        raise foreign
    return position


_STALE_SQL = """
SELECT id, title, domain, tier, owner, review_by, stale, status
  FROM kh.documents
 WHERE (stale OR review_by < current_date)
   AND (%(domain)s = '' OR domain = %(domain)s)
   AND (%(owner)s = '' OR owner = %(owner)s)
   AND (%(after_id)s::uuid IS NULL
        OR (%(after_review)s::date IS NOT NULL
            AND (review_by > %(after_review)s::date
                 OR (review_by = %(after_review)s::date AND id > %(after_id)s::uuid)
                 OR review_by IS NULL))
        OR (%(after_review)s::date IS NULL AND review_by IS NULL AND id > %(after_id)s::uuid))
 ORDER BY review_by ASC NULLS LAST, id ASC
 LIMIT %(n)s
"""


async def list_stale(
    call: ToolCall, domain: str = "", owner: str = "", limit: int = 50, cursor: str = ""
) -> dict[str, Any]:
    limit = max(1, min(MAX_LIST_LIMIT, limit))
    caller, key = call.caller, cursor_key(call.deps)
    scope = keyset_scope("list_stale", domain, owner)
    after_review: str | None = None
    after_id: str | None = None
    if cursor:
        after_review, after_id = decode_keyset(key, caller.sub, caller.audiences, scope, cursor, 2)
        try:
            if after_review is not None:
                datetime.date.fromisoformat(after_review)
            uuid.UUID(str(after_id))
        except (ValueError, TypeError):
            raise errors.KhError("cursor_foreign", errors.CURSOR_FOREIGN) from None
    async with call.conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _STALE_SQL,
            {
                "domain": domain,
                "owner": owner,
                "after_id": after_id,
                "after_review": after_review,
                "n": limit + 1,
            },
        )
        rows = await cur.fetchall()
    page = rows[:limit]
    next_cursor = ""
    if len(rows) > limit:
        last = page[-1]
        position = [_iso(last["review_by"]), str(last["id"])]
        next_cursor = encode_keyset(key, caller.sub, caller.audiences, scope, position)
    return {
        "documents": [
            {
                "citation_id": f"doc:{r['id']}",
                "title": r["title"],
                "domain": r["domain"],
                "tier": r["tier"],
                "owner": r["owner"] or "",
                "review_by": _iso(r["review_by"]),
                "stale": r["stale"],
                "status": r["status"],
            }
            for r in page
        ],
        "next_cursor": next_cursor,
    }


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="list_stale",
        description=(
            "List documents you may see that are marked stale or are past their review date, "
            "oldest review date first."
        ),
    )(tool_guard("list_stale", deps)(list_stale))
    server.tool(
        name="get_doc",
        description=(
            "Return one document, raw record or derived record by citation id "
            "(doc:<uuid>, raw:<uuid> or derived:<uuid>) with its tier, owner, approvals and freshness."
        ),
    )(tool_guard("get_doc", deps)(get_doc))
