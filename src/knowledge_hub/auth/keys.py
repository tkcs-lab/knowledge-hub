"""Hub keys for workflows (plan 3.7): ``khk_<id>.<secret>``, stored as the secret's SHA-256."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from typing import Any

from knowledge_hub.auth.claims import Caller

__all__ = ["KEY_RE", "KEY_TOOLS", "create_key", "new_key", "parse_key", "verify_key"]

KEY_RE = re.compile(r"^khk_([a-z2-7]{10})\.([A-Za-z0-9_-]{43})$")

# The tools registered by build_mcp (a unit test compares this with build_mcp).
KEY_TOOLS = (
    "delete_rows",
    "finish_sync",
    "get_doc",
    "ingest_record",
    "list_labels",
    "list_stale",
    "mark_record_outdated",
    "propose_doc",
    "search",
    "upsert_rows",
    "usage",
)


def new_key() -> tuple[str, str, bytes]:
    """Return ``(id, full key, sha256 of the secret)``."""
    key_id = base64.b32encode(secrets.token_bytes(7)).decode("ascii").lower()[:10]
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    return key_id, f"khk_{key_id}.{secret}", hashlib.sha256(secret.encode("ascii")).digest()


def parse_key(text: str) -> tuple[str, str] | None:
    """``(id, secret)`` of a well-formed key, otherwise ``None``."""
    m = KEY_RE.fullmatch(text) if isinstance(text, str) and text.isascii() else None
    return (m.group(1), m.group(2)) if m else None


async def create_key(
    conn: Any,
    *,
    group: str,
    tools: tuple[str, ...] | list[str],
    name: str,
    created_by: str,
    expires_days: int | None = None,
) -> str:
    """Insert a key row and return the full key (shown once). ``conn`` is an async connection."""
    key_id, full, digest = new_key()
    await conn.execute(
        "INSERT INTO kh.api_keys (id, secret_sha256, name, audience, tools, created_by, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s,"
        " CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s::int) END)",
        (key_id, digest, name, group, list(tools), created_by, expires_days, expires_days),
    )
    return full


async def verify_key(pool: Any, config: Any, text: str) -> Caller | None:
    parsed = parse_key(text)
    if parsed is None:
        return None
    key_id, secret = parsed
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT secret_sha256, audience, tools FROM kh.api_key_get(%s)", (key_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        stored, audience, tools = row
        given = hashlib.sha256(secret.encode("ascii")).digest()
        if not hmac.compare_digest(bytes(stored), given):
            return None
        if not config.is_active(audience):
            return None
        await conn.execute("SELECT kh.api_key_touch(%s)", (key_id,))
    return Caller(
        sub=f"key:{key_id}",
        principal_type="n8n_workflow",
        email=None,
        audiences=frozenset({audience}),
        token_id=key_id,
        tools=frozenset(tools),
    )
