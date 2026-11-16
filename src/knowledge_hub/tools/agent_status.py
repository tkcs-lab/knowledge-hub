"""``pr_status`` and ``list_my_proposals``: the caller's own agent proposals and notes (plan 3.2, 3.3, 3.7).

Both read ``kh.proposals`` with an explicit ``created_by_sub`` filter on top of RLS. ``pr_status`` has one
refusal for a malformed id, a missing row, a hidden row and another caller's row.
"""

import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer

from knowledge_hub.agent import texts
from knowledge_hub.approvals import BOT_LOGINS, current_approvals, requester_from_statuses
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import KhError, ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.proposals import REQUESTER_CONTEXT

MIN_LIMIT, MAX_LIMIT = 1, 50
_OPEN_STATES = ("pending", "pr_open", "committed")
_CI_CONTEXTS = ("kh/validate", "kh/domain-owners")

_COLS = "id, kind, state, repo, path, doc_key, title, branch, commit_sha, pr_number, pr_url, created_at"
_ROW_SQL = f"SELECT {_COLS} FROM kh.proposals WHERE id = %s AND created_by_sub = %s"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _not_found() -> KhError:
    return kh_error("not_found", errors.NOT_FOUND)


def _ci_logins(deps: Deps) -> set[str]:
    return {x.strip().casefold() for x in deps.settings.ci_status_logins.split(",") if x.strip()}


def _base(row: tuple[Any, ...]) -> dict[str, Any]:
    pid, kind, state, repo, path, _key, title, _branch, _commit, number, url, _created = row
    return {
        "proposal_id": str(pid),
        "kind": kind,
        "repo": repo,
        "path": path,
        "title": title,
        "state": state,
        "pr_number": number,
        "pr_url": url,
    }


async def _indexed_note(call: ToolCall, repo: str, doc_key: str, commit_sha: str | None) -> bool:
    if not commit_sha:
        return False
    cur = await call.conn.execute(
        "SELECT 1 FROM kh.documents WHERE repo = %s AND doc_key = %s AND commit_sha = %s LIMIT 1",
        (repo, doc_key, commit_sha),
    )
    return await cur.fetchone() is not None


async def _indexed_merge(call: ToolCall, repo: str, number: int) -> bool:
    cur = await call.conn.execute(
        "SELECT 1 FROM kh.documents WHERE repo = %s AND merge_pr = %s LIMIT 1", (repo, number)
    )
    return await cur.fetchone() is not None


def _newest_ci_state(statuses: list[dict[str, Any]], context: str, logins: set[str]) -> str:
    # Forgejo lists statuses newest first; the first one from a CI account wins.
    for s in statuses:
        creator = s.get("creator")
        if s.get("context") == context and isinstance(creator, str) and creator.casefold() in logins:
            return str(s.get("state") or "missing")
    return "missing"


async def pr_status(call: ToolCall, proposal_id: str) -> dict[str, Any]:
    deps, caller = call.deps, call.caller
    try:
        pid = uuid.UUID(proposal_id)
    except ValueError:
        raise _not_found() from None
    cur = await call.conn.execute(_ROW_SQL, (pid, caller.sub))
    row = await cur.fetchone()
    if row is None:
        raise _not_found()
    out = _base(row)
    _, kind, _state, repo, _path, doc_key, _title, branch, commit_sha, number, _url, _created = row
    out.update(
        {"checks": {}, "approved_by": [], "requester_approval_ignored": False, "merged": False,
         "merge_commit": None, "indexed": False}
    )  # fmt: skip
    if kind == "note":
        out["indexed"] = await _indexed_note(call, repo, doc_key, commit_sha)
        return out
    if number is None:
        return out
    forgejo = deps.forgejo
    if forgejo is None:
        raise KhError("no_forgejo", texts.NO_NOTES_FORGEJO)
    pull = await forgejo.pull(repo, number)
    head = pull.get("head") or {}
    if head.get("ref") != branch:
        raise _not_found()
    merged = bool(pull.get("merged"))
    out["state"] = "merged" if merged else ("closed" if pull.get("state") == "closed" else "open")
    out["merged"] = merged
    out["merge_commit"] = pull.get("merge_commit_sha")
    statuses = await forgejo.commit_statuses(repo, head.get("sha") or "")
    requesters, _problem = requester_from_statuses(pull, statuses)
    logins = _ci_logins(deps)
    checks = {REQUESTER_CONTEXT: "success" if requesters else "missing"}
    for context in _CI_CONTEXTS:
        checks[context] = _newest_ci_state(statuses, context, logins)
    out["checks"] = checks
    reviews = await forgejo.pull_reviews(repo, number)
    counted = current_approvals(pull, reviews, BOT_LOGINS, exclude_logins=requesters)
    out["approved_by"] = sorted(r["user"]["login"] for r in counted)
    everyone = {
        r["user"]["login"].casefold() for r in current_approvals(pull, reviews, BOT_LOGINS)
    }
    out["requester_approval_ignored"] = any(r.casefold() in everyone for r in requesters)
    out["indexed"] = await _indexed_merge(call, repo, number)
    return out


async def list_my_proposals(call: ToolCall, only_open: bool = False, limit: int = 20) -> dict[str, Any]:
    if not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise KhError("bad_limit", texts.BAD_LIMIT)
    sql = f"SELECT {_COLS} FROM kh.proposals WHERE created_by_sub = %s"
    params: list[Any] = [call.caller.sub]
    if only_open:
        sql += " AND state = ANY(%s)"
        params.append(list(_OPEN_STATES))
    sql += " ORDER BY created_at DESC, id LIMIT %s"
    params.append(limit)
    cur = await call.conn.execute(sql, params)
    items = []
    for row in await cur.fetchall():
        item = _base(row)
        item["created_at"] = _iso(row[11])
        items.append(item)
    return {"proposals": items}


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="list_my_proposals",
        description="List your own proposals and agent notes, newest first.",
    )(tool_guard("list_my_proposals", deps)(list_my_proposals))
    server.tool(
        name="pr_status",
        description="Show the state, checks and approvals of one of your own proposals.",
    )(tool_guard("pr_status", deps)(pr_status))
