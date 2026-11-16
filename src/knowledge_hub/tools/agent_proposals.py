"""``propose_doc``: open a pull request that proposes a new canonical document (plan 16, 3.2 to 3.4, 3.8).

The bot never writes to ``main``: it creates ``bot/propose/<proposal-id>``, commits one file there,
records the requester on that commit and opens a pull request. ``submit`` is shared with the other
proposal tools. Row writes use their own committed caller transactions (see ``agent/store.py``), so a
failed Forgejo write leaves a ``failed`` row for the retry; the transaction ``tool_guard`` opened is
used for reads only.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from psycopg import errors as pg_errors

from knowledge_hub.agent import texts
from knowledge_hub.agent.guards import (
    check_citations,
    record_write_failure,
    require_audience,
    require_requester,
    t0_repo,
)
from knowledge_hub.agent.render import (
    commit_message,
    content_sha,
    dedupe_key,
    pr_body,
    render_file,
    verify_roundtrip,
)
from knowledge_hub.agent.rules import (
    Requester,
    check_body,
    check_entities,
    check_id,
    check_rationale,
    check_review_by,
    check_title,
    doc_path,
    next_day_text,
)
from knowledge_hub.agent.store import (
    Existing,
    count_candidates_today,
    count_today,
    find_by_key,
    insert_proposal,
    mark,
)
from knowledge_hub.approvals import requester_from_statuses
from knowledge_hub.forgejo.client import ForgejoError
from knowledge_hub.governance.owners import OwnersError, load_owners
from knowledge_hub.governance.rules import expected_type
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import KhError, ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.proposals import PROPOSE_REF_PREFIX, REQUESTER_POSTERS
from knowledge_hub.tools import promote

PENDING_GRACE = timedelta(minutes=5)
LABELS = ("promotion", "domain/{domain}")
_AGENT_KINDS = ["doc", "edit"]


def _failed(tool: str) -> KhError:
    return kh_error("proposal_failed", texts.PROPOSAL_FAILED, tool=tool)


def _same_login(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _result(row: Existing) -> dict[str, Any]:
    return {
        "proposal_id": str(row.id),
        "branch": row.branch,
        "pr_number": row.pr_number,
        "pr_url": row.pr_url,
        "existing": True,
    }


async def _adopt(call: ToolCall, bot: Any, repo: str, row: Existing, requester: Requester) -> Existing | None:
    """Mark a ``pending`` row ``pr_open`` when its branch already has this requester's open pull request."""
    if not row.branch:
        return None
    pull = await bot.pull_for_head(repo, row.branch)
    if pull is None:
        return None
    head = pull.get("head") or {}
    author = (pull.get("user") or {}).get("login")
    if (
        pull.get("state") != "open"
        or author not in REQUESTER_POSTERS
        or (pull.get("base") or {}).get("ref") != "main"
        or not isinstance(head.get("sha"), str)
    ):
        return None
    logins, problem = requester_from_statuses(pull, await bot.commit_statuses(repo, head["sha"]))
    if problem is not None or len(logins) != 1 or not _same_login(logins[0], requester.login):
        return None
    number, url = int(pull["number"]), str(pull["html_url"])
    await mark(
        call.deps, call.caller, row.id, "pr_open", branch=row.branch, commit_sha=head["sha"], pr=(number, url)
    )
    return replace(row, state="pr_open", commit_sha=head["sha"], pr_number=number, pr_url=url)


async def _settle(
    call: ToolCall, bot: Any, tool: str, repo: str, row: Existing, requester: Requester, now: datetime
) -> dict[str, Any] | None:
    """Answer from an open row of the same proposal, or ``None`` when a new proposal may be made."""
    if row.state == "pr_open":
        return _result(row)
    try:
        adopted = await _adopt(call, bot, repo, row, requester)
    except (ForgejoError, KeyError, TypeError, ValueError) as exc:
        record_write_failure(tool, exc)
        raise _failed(tool) from None
    if adopted is not None:
        return _result(adopted)
    if now - row.created_at < PENDING_GRACE:
        raise _failed(tool)  # another call may be running
    await mark(
        call.deps, call.caller, row.id, "failed", branch=row.branch, error="abandoned pending proposal"
    )
    return None


async def submit(
    call: ToolCall,
    *,
    tool: str,
    kind: str,
    repo: str,
    path: str,
    doc_key: str,
    domain: str,
    title: str,
    subject: str,
    text: str,
    rationale: str,
    requester: Requester,
    base_sha: str,
) -> dict[str, Any]:
    deps, caller, conn = call.deps, call.caller, call.conn
    bot = deps.forgejo_bot
    now = datetime.now(UTC)
    key = dedupe_key(caller.sub, repo, path, kind, content_sha(text))

    existing = await find_by_key(conn, key)
    if existing is not None:
        settled = await _settle(call, bot, tool, repo, existing, requester, now)
        if settled is not None:
            return settled

    limit = deps.settings.agent_proposal_daily_limit
    if await count_today(conn, kinds=_AGENT_KINDS, now=now, login=requester.login) >= limit:
        raise KhError("limit", texts.PROPOSAL_LIMIT.format(n=limit, time=next_day_text(now)))
    shared = deps.settings.promotion_daily_limit
    used = await count_candidates_today(conn, domain, repo, now) + await count_today(
        conn, kinds=_AGENT_KINDS, now=now, domain=domain, repo=repo
    )
    if used >= shared:
        raise kh_error("limit", errors.LIMIT, d=domain, repo=repo, n=shared, time=next_day_text(now))

    pid = uuid.uuid4()
    branch = PROPOSE_REF_PREFIX + str(pid)
    try:
        await insert_proposal(
            deps,
            caller,
            id=pid,
            kind=kind,
            repo=repo,
            path=path,
            doc_key=doc_key,
            domain=domain,
            title=title,
            content_sha=content_sha(text),
            requester_login=requester.login,
            dedupe_key=key,
            branch=branch,
            base_sha=base_sha,
        )
    except pg_errors.UniqueViolation:
        # A concurrent call inserted the same proposal first: answer from its row.
        raced = await find_by_key(conn, key)
        settled = None if raced is None else await _settle(call, bot, tool, repo, raced, requester, now)
        if settled is None:
            raise _failed(tool) from None
        return settled

    sha: str | None = None
    try:
        await bot.create_branch(repo, branch, base_sha)
        written = await bot.put_file(
            repo, branch, path, text, commit_message(subject, tool, requester, str(pid))
        )
        sha = written["sha"]
        changed = await bot.compare_files(repo, base_sha, sha)
        if (
            written["parents"] != [base_sha]
            or [c.get("filename") for c in changed] != [path]
            or await bot.head_sha(repo, branch) != sha
        ):
            # A foreign commit reached the branch: post nothing, the proposal must be made again.
            raise ForgejoError(f"branch {branch} does not hold exactly the proposed commit")
        await bot.post_requester(repo, sha, requester.login, str(pid))
        pull = await bot.open_pull(
            repo,
            branch,
            "main",
            title,
            pr_body(title, path, rationale, tool, requester, str(pid), base_sha),
            [lbl.format(domain=domain) for lbl in LABELS],
        )
        number, url = int(pull["number"]), str(pull["html_url"])
    except (ForgejoError, KeyError, TypeError, ValueError) as exc:
        record_write_failure(tool, exc)
        await mark(deps, caller, pid, "failed", branch=branch, commit_sha=sha, error=str(exc)[:500])
        raise _failed(tool) from None
    await mark(deps, caller, pid, "pr_open", branch=branch, commit_sha=sha, pr=(number, url))
    return {
        "proposal_id": str(pid),
        "branch": branch,
        "pr_number": number,
        "pr_url": url,
        "existing": False,
    }


async def propose_doc(
    call: ToolCall,
    audience: str,
    domain: str,
    product: str,
    kind: Literal["requirements", "specs", "guides"],
    id: str,
    title: str,
    review_by: str,
    body: str,
    rationale: str,
    entities: list[str] = [],  # noqa: B006 - never mutated
    component: str = "",
) -> dict[str, Any]:
    deps, config, settings = call.deps, call.deps.config, call.deps.settings
    if not settings.agent_proposals_enabled:
        raise KhError("disabled_proposals", texts.DISABLED_PROPOSALS)
    bot = deps.forgejo_bot
    if bot is None or deps.forgejo is None:
        raise KhError("no_forgejo", promote.NO_FORGEJO)
    requester = await require_requester(call, deps.forgejo)
    require_audience(call.caller, audience)
    repo = t0_repo(config, audience)
    if domain not in config.domains:
        raise KhError("bad_domain", "domain must be one of " + ", ".join(config.domains))
    check_id(id)
    title = check_title(title)
    due = check_review_by(review_by, datetime.now(UTC).date())
    body = check_body(body, settings.agent_max_body_bytes)
    rationale = check_rationale(rationale)
    refs = check_entities(entities, config, config.repos[repo].audience)
    path, layout = doc_path(config, domain, product, component, kind, id)
    await check_citations(call.conn, config, repo, title, body, rationale)

    try:
        head = await bot.head_sha(repo, "main")
        exists = await bot.path_exists(repo, path, head)
    except ForgejoError as exc:
        raise promote._internal("head_sha", exc) from None
    if exists:
        raise KhError("doc_exists", texts.DOC_EXISTS)
    cur = await call.conn.execute(
        "SELECT 1 FROM kh.documents WHERE repo = %s AND doc_key = %s AND path <> %s LIMIT 1",
        (repo, id, path),
    )
    if await cur.fetchone() is not None:
        raise KhError("id_in_use", texts.ID_IN_USE.format(id=id, repo=repo))

    try:
        owners = load_owners(await bot.get_raw(repo, "owners.yml", head))
        team = owners.domains.get(domain)
    except (OwnersError, ForgejoError) as exc:
        raise promote._internal("owners.yml", exc) from None
    if team is None:
        raise promote._internal("owners.yml", KeyError(domain))

    text = render_file(
        id=id,
        title=title,
        domain=domain,
        review_by=due,
        type=expected_type(layout),
        owner=team,
        entities=refs,
        body=body,
    )
    try:
        verify_roundtrip(
            text,
            path,
            config,
            id=id,
            title=title,
            domain=domain,
            review_by=due,
            type=expected_type(layout),
            owner=team,
            entities=refs,
            body=body,
        )
    except ValueError as exc:
        raise promote._internal("render", exc) from None
    return await submit(
        call,
        tool="propose_doc",
        kind="doc",
        repo=repo,
        path=path,
        doc_key=id,
        domain=domain,
        title=title,
        subject=f"Propose {path}",
        text=text,
        rationale=rationale,
        requester=requester,
        base_sha=head,
    )


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="propose_doc",
        description=(
            "Propose a new canonical document as a pull request for human review. Give the audience, "
            "domain, product, optional component, kind and id explicitly; the hub builds the path, type "
            "and owner. The proposal is reviewed in Forgejo by owners other than you."
        ),
    )(tool_guard("propose_doc", deps)(propose_doc))
