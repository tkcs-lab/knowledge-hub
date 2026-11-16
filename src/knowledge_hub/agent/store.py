"""The ``kh.proposals`` store of the agent write tools (plan sections 3.5, 3.8).

Reads take the tool's open ``conn``. Writes use their own committed caller transactions, so a
failed Forgejo write leaves a ``failed`` row for the retry (the transaction ``tool_guard`` opened
is rolled back when a tool raises).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from knowledge_hub.agent.rules import day_start
from knowledge_hub.auth.claims import Caller
from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import KhError, kh_error
from knowledge_hub.mcp.server import Deps

log = logging.getLogger("knowledge_hub.agent")

_INSERT = """
INSERT INTO kh.proposals (id, kind, state, repo, path, doc_key, domain, title, content_sha, dedupe_key,
    branch, base_sha, requester_login, created_by_sub, required_audiences, allowed_emails)
VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
"""
_MARK = "SELECT kh.proposal_mark(%s, %s, %s, %s, %s, %s, %s)"


@dataclass(frozen=True)
class Existing:
    id: uuid.UUID
    state: str
    branch: str | None
    commit_sha: str | None
    pr_number: int | None
    pr_url: str | None
    created_at: datetime


def _internal(why: str) -> KhError:
    ref = uuid.uuid4()
    log.error("agent store failed ref=%s: %s", ref, why)
    return kh_error("internal", errors.INTERNAL, ref=ref)


async def find_by_key(conn: Any, key: str) -> Existing | None:
    cur = await conn.execute(
        "SELECT id, state, branch, commit_sha, pr_number, pr_url, created_at FROM kh.proposals"
        " WHERE dedupe_key = %s AND state IN ('pending', 'pr_open') ORDER BY created_at DESC LIMIT 1",
        (key,),
    )
    row = await cur.fetchone()
    return None if row is None else Existing(*row)


async def count_today(
    conn: Any,
    *,
    kinds: list[str],
    now: datetime,
    domain: str | None = None,
    repo: str | None = None,
    login: str | None = None,
) -> int:
    cur = await conn.execute(
        "SELECT kh.count_proposals_today(%s::text[], %s, %s, %s, %s)",
        (kinds, domain, repo, login, day_start(now)),
    )
    (n,) = await cur.fetchone()
    return int(n)


async def count_candidates_today(conn: Any, domain: str, repo: str, now: datetime) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM kh.candidates WHERE domain = %s AND target_repo = %s AND created_at >= %s",
        (domain, repo, day_start(now)),
    )
    (n,) = await cur.fetchone()
    return int(n)


async def note_written_by(conn: Any, repo: str, path: str, sub: str) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM kh.proposals WHERE kind = 'note' AND state IN ('committed', 'failed')"
        " AND repo = %s AND path = %s AND created_by_sub = %s LIMIT 1",
        (repo, path, sub),
    )
    return await cur.fetchone() is not None


async def find_unsettled_note(
    conn: Any, repo: str, path: str, sub: str, content_sha: str
) -> uuid.UUID | None:
    cur = await conn.execute(
        "SELECT id FROM kh.proposals WHERE kind = 'note' AND state IN ('failed', 'pending')"
        " AND repo = %s AND path = %s AND created_by_sub = %s AND content_sha = %s"
        " ORDER BY created_at DESC LIMIT 1",
        (repo, path, sub, content_sha),
    )
    row = await cur.fetchone()
    return None if row is None else row[0]


async def insert_proposal(
    deps: Deps,
    caller: Caller,
    *,
    id: uuid.UUID,
    kind: str,
    repo: str,
    path: str,
    doc_key: str,
    domain: str,
    title: str,
    content_sha: str,
    requester_login: str,
    dedupe_key: str | None = None,
    branch: str | None = None,
    base_sha: str | None = None,
) -> None:
    audiences = [deps.config.repos[repo].audience]
    async with caller_tx(deps.app_pool, caller) as conn:
        await conn.execute(
            _INSERT,
            (
                id, kind, repo, path, doc_key, domain, title, content_sha, dedupe_key, branch, base_sha,
                requester_login, caller.sub, audiences,
            ),
        )  # fmt: skip


async def mark(
    deps: Deps,
    caller: Caller,
    id: uuid.UUID,
    state: str,
    *,
    branch: str | None = None,
    commit_sha: str | None = None,
    pr: tuple[int, str] | None = None,
    error: str | None = None,
) -> None:
    number, url = pr if pr is not None else (None, None)
    async with caller_tx(deps.app_pool, caller) as conn:
        cur = await conn.execute(_MARK, (id, state, branch, commit_sha, number, url, error))
        row = await cur.fetchone()
    if not row or row[0] is not True:
        raise _internal(f"proposal_mark refused {state}")
