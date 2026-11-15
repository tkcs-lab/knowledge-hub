"""``propose_change``: open a pull request that promotes cited knowledge into a canonical repo (plan 3.8).

The bot never writes to ``main``: it creates ``bot/promote/<candidate-id>``, commits one file there and
opens a pull request. Candidate state changes go through ``kh.candidate_mark`` only.

``tool_guard`` rolls the caller transaction back when a tool raises, but a failed pull request must
leave a ``failed`` candidate behind for the retry. So the candidate is inserted and marked in their
own committed caller transactions; the transaction ``tool_guard`` opened is used for reads only.
"""

import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer
from psycopg import errors as pg_errors

from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.forgejo.client import ForgejoError
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import KhError, ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools.derived import _ID, _parse_ids
from knowledge_hub.visibility import Visibility, route_target_repo

log = logging.getLogger("knowledge_hub.tools")

MAX_SOURCES = 50
BRANCH_PREFIX = "bot/promote/"
LABELS = ("promotion", "domain/{domain}")
_PATH = re.compile(r"^(portfolio|product|technical)/[a-z0-9][a-z0-9/_-]*\.md$")

PR_TEMPLATE = """## What

{what}

## Why

{why}

## Sources

{sources}

## Candidate

{candidate}
"""

# Local fixed texts (the catalogue has no entry for these).
NO_TARGET = "give exactly one of target_id and new_path"
NO_SOURCES = "give at least one source id"
NO_FORGEJO = "promotion is not available: no Forgejo connection is configured"

_INSERT = """
INSERT INTO kh.candidates (id, derived_id, target_doc_id, state, domain, target_repo, target_path, title,
    body_sha, dedupe_key, branch, created_by_sub, required_audiences, allowed_emails)
VALUES (%s, %s, %s, 'proposed', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_INSERT_SOURCE = """
INSERT INTO kh.candidate_sources (candidate_id, source_kind, source_id, src_required_audiences)
VALUES (%s, %s, %s, '{}')
"""
_MARK = "SELECT kh.candidate_mark(%s, %s, %s, %s, %s, %s)"


def normalise(content: str) -> str:
    """CRLF to LF, trailing whitespace off every line, leading and trailing blank lines off."""
    lines = [ln.rstrip() for ln in content.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def dedupe_key(repo: str, path: str, source_ids: list[str], body_sha: str) -> str:
    text = "\n".join([repo, path, ",".join(sorted(source_ids)), body_sha])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bad_path() -> KhError:
    return KhError("bad_path", errors.BAD_PATH)


def _internal(why: str, exc: BaseException | None = None) -> KhError:
    ref = uuid.uuid4()
    log.error("propose_change failed ref=%s: %s %s", ref, why, exc if exc is not None else "")
    return kh_error("internal", errors.INTERNAL, ref=ref)


def _pr_body(title: str, path: str, rationale: str, source_ids: list[str], candidate_id: uuid.UUID) -> str:
    return PR_TEMPLATE.format(
        what=f"{title}\n\n`{path}`",
        why=rationale,
        sources="\n".join(f"- {s}" for s in source_ids),
        candidate=str(candidate_id),
    )


async def _target_doc(conn: Any, target_id: str) -> tuple[uuid.UUID, str, str]:
    m = _ID.match(target_id)
    if m is None or m.group(1) != "doc" or m.group(3) is not None:
        raise kh_error("bad_id", errors.BAD_ID, x=target_id[:100])
    doc_id = uuid.UUID(m.group(2))
    cur = await conn.execute("SELECT repo, path FROM kh.documents WHERE id = %s", (doc_id,))
    row = await cur.fetchone()
    if row is None:
        raise KhError("not_found", errors.NOT_FOUND)
    repo, path = row
    if _PATH.match(path) is None or ".." in path or "//" in path:
        raise _bad_path()
    return doc_id, repo, path


async def _retry_candidate(conn: Any, caller_sub: str, candidate_id: str) -> tuple[uuid.UUID, str, str]:
    bad = KhError("bad_candidate", errors.BAD_CANDIDATE)
    try:
        cid = uuid.UUID(candidate_id)
    except ValueError:
        raise bad from None
    cur = await conn.execute(
        "SELECT dedupe_key, branch FROM kh.candidates WHERE id = %s AND state = 'failed'"
        " AND created_by_sub = %s",
        (cid, caller_sub),
    )
    row = await cur.fetchone()
    if row is None:
        raise bad
    return cid, row[0], row[1]


async def _mark(
    deps: Deps,
    caller: Any,
    cid: uuid.UUID,
    state: str,
    branch: str,
    pr: tuple[int, str] | None,
    err: str | None,
) -> None:
    number, url = pr if pr is not None else (None, None)
    async with caller_tx(deps.app_pool, caller) as conn:
        cur = await conn.execute(_MARK, (cid, state, branch, number, url, err))
        row = await cur.fetchone()
    if not row or row[0] is not True:
        raise _internal(f"candidate_mark refused {state}")


async def propose_change(
    call: ToolCall,
    domain: str,
    title: str,
    content: str,
    rationale: str,
    source_ids: list[str],
    target_id: str = "",
    new_path: str = "",
    candidate_id: str = "",
) -> dict[str, Any]:
    deps, caller, conn = call.deps, call.caller, call.conn
    if bool(target_id) == bool(new_path):
        raise KhError("no_target", NO_TARGET)
    if domain not in deps.config.domains:
        raise KhError("bad_domain", "domain must be one of " + ", ".join(deps.config.domains))
    parsed = _parse_ids(source_ids)
    if not parsed:
        raise KhError("no_sources", NO_SOURCES)
    if len(parsed) > MAX_SOURCES:
        raise KhError("too_many_sources", errors.TOO_MANY_SOURCES)
    if any(v is not None for _, _, v in parsed):
        raise KhError("bad_version", errors.BAD_VERSION)
    ids = [f"{k}:{u}" for k, u, _ in parsed]

    if new_path:
        if (
            _PATH.match(new_path) is None
            or not new_path.startswith(domain + "/")
            or ".." in new_path
            or "//" in new_path
        ):
            raise _bad_path()
        target_doc: uuid.UUID | None = None
        explicit_repo: str | None = None
        path = new_path
    else:
        target_doc, explicit_repo, path = await _target_doc(conn, target_id)

    cur = await conn.execute(
        "SELECT n_resolved, required_audiences, allowed_emails FROM kh.strict_visibility(%s::text[])", (ids,)
    )
    n_resolved, audiences, emails = await cur.fetchone()
    if n_resolved != len(ids):
        # Missing and hidden ids are indistinguishable.
        raise KhError("not_found", errors.NOT_FOUND)
    repo = route_target_repo(
        [
            Visibility(
                required_audiences=list(audiences), allowed_emails=None if emails is None else list(emails)
            )
        ],
        deps.config,
        explicit_repo,
    )

    forgejo = deps.forgejo
    if forgejo is None:
        raise KhError("no_forgejo", NO_FORGEJO)
    if new_path:
        try:
            exists = await forgejo.path_exists(repo, path, "main")
        except ForgejoError as exc:
            raise _internal("path_exists", exc) from None
        if exists:
            raise _bad_path()

    text = normalise(content)
    body_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    key = dedupe_key(repo, path, ids, body_sha)

    if candidate_id:
        cid, stored_key, branch = await _retry_candidate(conn, caller.sub, candidate_id)
        if stored_key != key or branch != BRANCH_PREFIX + str(cid):
            # A retry re-sends the same proposal; a different one would not match the stored visibility.
            raise KhError("bad_candidate", errors.BAD_CANDIDATE)
        retry = True
    else:
        retry = False
        cur = await conn.execute(
            "SELECT id, pr_number FROM kh.candidates"
            " WHERE dedupe_key = %s AND state IN ('proposed', 'pr_open')",
            (key,),
        )
        dup = await cur.fetchone()
        if dup is not None:
            raise kh_error("duplicate", errors.DUPLICATE, id=dup[0], n=dup[1] if dup[1] is not None else "?")
        now = datetime.now(UTC)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cur = await conn.execute(
            "SELECT count(*) FROM kh.candidates WHERE domain = %s AND target_repo = %s AND created_at >= %s",
            (domain, repo, midnight),
        )
        (used,) = await cur.fetchone()
        limit = deps.settings.promotion_daily_limit
        if used >= limit:
            raise kh_error(
                "limit",
                errors.LIMIT,
                d=domain,
                repo=repo,
                n=limit,
                time=(midnight + timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
            )
        cid = uuid.uuid4()
        branch = BRANCH_PREFIX + str(cid)
        derived = [u for k, u, _ in parsed if k == "derived"]
        try:
            async with caller_tx(deps.app_pool, caller) as w:
                await w.execute(
                    _INSERT,
                    (
                        cid,
                        uuid.UUID(derived[0]) if len(derived) == 1 else None,
                        target_doc,
                        domain,
                        repo,
                        path,
                        title,
                        body_sha,
                        key,
                        branch,
                        caller.sub,
                        list(audiences),
                        None,
                    ),
                )
                for k, u, _ in parsed:
                    await w.execute(_INSERT_SOURCE, (cid, k, u))
                try:
                    # Run the deferred visibility check now so a failure is an INTERNAL error with a reason.
                    await w.execute("SET CONSTRAINTS kh.candidate_visibility_matches IMMEDIATE")
                except psycopg.Error as exc:
                    raise _internal(f"visibility check sqlstate={exc.sqlstate}", exc) from None
        except pg_errors.UniqueViolation:
            cur = await conn.execute(
                "SELECT id, pr_number FROM kh.candidates WHERE dedupe_key = %s"
                " AND state IN ('proposed', 'pr_open')",
                (key,),
            )
            dup = await cur.fetchone()
            if dup is None:
                raise _internal("dedupe collision with a row the caller cannot see") from None
            raise kh_error(
                "duplicate", errors.DUPLICATE, id=dup[0], n=dup[1] if dup[1] is not None else "?"
            ) from None

    try:
        head = await forgejo.head_sha(repo, "main")
        try:
            await forgejo.create_branch(repo, branch, head)
        except ForgejoError:
            if not retry:
                raise  # on a retry the branch may exist from the failed attempt
        await forgejo.put_file(repo, branch, path, text + "\n", f"Propose {path} (candidate {cid})")
        pull = await forgejo.open_pull(
            repo,
            branch,
            "main",
            title,
            _pr_body(title, path, rationale, ids, cid),
            [lbl.format(domain=domain) for lbl in LABELS],
        )
        number, url = int(pull["number"]), str(pull["html_url"])
    except (ForgejoError, KeyError, TypeError, ValueError) as exc:
        log.error("pull request for candidate %s failed: %s", cid, exc)
        await _mark(deps, caller, cid, "failed", branch, None, str(exc)[:500])
        raise kh_error("pr_failed", errors.PR_FAILED, id=cid) from None
    await _mark(deps, caller, cid, "pr_open", branch, (number, url), None)
    return {"candidate_id": str(cid), "branch": branch, "pr_number": number, "pr_url": url}


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="propose_change",
        description=(
            "Propose a change to a canonical document as a pull request for human review. Give exactly "
            "one of target_id (a doc: id to change) and new_path (a new markdown file in the domain's "
            "folder), the full new content, a rationale and the doc:, raw: or derived: ids it rests on. "
            "The proposal goes to the canonical repository whose audience matches the sources. Pass "
            "candidate_id to retry a proposal that failed."
        ),
    )(tool_guard("propose_change", deps)(propose_change))
