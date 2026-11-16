"""``write_note``: write or update an unreviewed agent note in the T1 notes repo of an audience (plan 3.5).

The note lands on ``main`` of the notes repository as ``agent/<id>.md`` through the ``notes`` Forgejo
client. ``tool_guard`` rolls the caller transaction back when a tool raises, but a failed write must
leave a ``failed`` row behind for the retry, so rows are written through ``agent.store`` (own committed
transactions); the transaction ``tool_guard`` opened is used for reads only.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

from knowledge_hub.agent import texts
from knowledge_hub.agent.guards import (
    check_citations,
    record_write_failure,
    require_audience,
    require_requester,
    t1_repo,
)
from knowledge_hub.agent.render import commit_message, content_sha, render_file, verify_roundtrip
from knowledge_hub.agent.rules import (
    check_body,
    check_commit,
    check_entities,
    check_id,
    check_markup,
    check_review_by,
    check_title,
    next_day_text,
    note_path,
)
from knowledge_hub.agent.store import (
    count_today,
    find_unsettled_note,
    insert_proposal,
    mark,
    note_written_by,
)
from knowledge_hub.forgejo.client import ForgejoError
from knowledge_hub.indexer.frontmatter import Rejected, parse_document
from knowledge_hub.mcp.errors import KhError, ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps

INDEXED = "after the next index run"


async def write_note(
    call: ToolCall,
    audience: str,
    id: str,
    title: str,
    domain: str,
    review_by: str,
    body: str,
    entities: list[str] = [],  # noqa: B006 - never mutated
    base_commit: str = "",
) -> dict[str, Any]:
    deps, caller, conn = call.deps, call.caller, call.conn
    settings, config = deps.settings, deps.config
    if not settings.agent_notes_enabled:
        raise KhError("disabled_notes", texts.DISABLED_NOTES)
    notes = deps.forgejo_notes
    if notes is None or deps.forgejo is None:
        raise KhError("no_forgejo", texts.NO_NOTES_FORGEJO)
    requester = await require_requester(call, deps.forgejo)
    require_audience(caller, audience)
    repo = t1_repo(config, audience)
    if domain not in config.domains:
        raise KhError("bad_domain", "domain must be one of " + ", ".join(config.domains))
    check_id(id)
    title = check_title(title)
    check_markup(title)
    now = datetime.now(UTC)
    review = check_review_by(review_by, now.date())
    ents = check_entities(entities, config, config.repos[repo].audience)
    body = check_body(body, settings.agent_max_body_bytes)
    check_markup(body)
    check_commit(base_commit)
    await check_citations(conn, config, repo, title, body)

    path = note_path(id)
    text = render_file(
        id=id,
        title=title,
        domain=domain,
        review_by=review,
        type="agent-note",
        owner=requester.login,
        entities=ents,
        body=body,
    )
    verify_roundtrip(
        text,
        path,
        config,
        id=id,
        title=title,
        domain=domain,
        review_by=review,
        type="agent-note",
        owner=requester.login,
        entities=ents,
        body=body,
    )

    head = await notes.head_sha(repo, "main")
    tree = await notes.list_tree(repo, head)
    for entry in tree:
        p = str(entry.get("path", ""))
        if (
            entry.get("type") == "blob"
            and p.endswith(".md")
            and not p.startswith("agent/")
            and p.rsplit("/", 1)[-1][:-3] == id
        ):
            raise KhError("id_in_use", texts.ID_IN_USE.format(id=id, repo=repo))
    cur = await conn.execute(
        "SELECT 1 FROM kh.documents WHERE repo = %s AND doc_key = %s AND path <> %s", (repo, id, path)
    )
    if await cur.fetchone() is not None:
        raise KhError("id_in_use", texts.ID_IN_USE.format(id=id, repo=repo))

    if await notes.path_exists(repo, path, head):
        last = await notes.last_commit(repo, path, head)
        current = await notes.get_raw(repo, path, head)
        parsed = parse_document(current, path, config.domains)
        if (
            isinstance(parsed, Rejected)
            or parsed.type != "agent-note"
            or parsed.owner != requester.login
            or not await note_written_by(conn, repo, path, caller.sub)
        ):
            raise KhError("not_yours", texts.NOT_YOURS)
        if current == text:
            leftover = await find_unsettled_note(conn, repo, path, caller.sub, content_sha(text))
            if leftover is not None:
                await mark(deps, caller, leftover, "committed", commit_sha=last)
            return {
                "proposal_id": "",
                "repo": repo,
                "path": path,
                "commit_sha": last,
                "created": False,
                "unchanged": True,
                "indexed": INDEXED,
            }
        if not base_commit:
            raise KhError("note_exists", texts.NOTE_EXISTS)
        if base_commit != last:
            raise KhError("note_changed", texts.NOTE_CHANGED.format(sha=last))
    elif base_commit:
        raise KhError("note_missing", texts.NOTE_MISSING)

    limit = settings.agent_note_daily_limit
    if await count_today(conn, kinds=["note"], now=now, login=requester.login) >= limit:
        raise KhError("limit", texts.NOTE_LIMIT.format(n=limit, time=next_day_text(now)))

    pid = uuid.uuid4()
    await insert_proposal(
        deps,
        caller,
        id=pid,
        kind="note",
        repo=repo,
        path=path,
        doc_key=id,
        domain=domain,
        title=title,
        content_sha=content_sha(text),
        requester_login=requester.login,
    )
    try:
        prev = await notes.blob_sha(repo, path, head)
        commit = await notes.put_note(
            repo, path, text, commit_message(f"Agent note {id}", "write_note", requester, str(pid)), prev
        )
    except ForgejoError as exc:
        record_write_failure("write_note", exc)
        await mark(deps, caller, pid, "failed", error=str(exc)[:500])
        raise kh_error("note_failed", texts.NOTE_FAILED) from None
    await mark(deps, caller, pid, "committed", commit_sha=commit)
    return {
        "proposal_id": str(pid),
        "repo": repo,
        "path": path,
        "commit_sha": commit,
        "created": prev is None,
        "unchanged": False,
        "indexed": INDEXED,
    }


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="write_note",
        description=(
            "Write or update an unreviewed agent note in the notes repository of an audience. Give the "
            "audience explicitly. A note is stored as type agent-note under agent/, owned by you, and is "
            "ranked below reviewed documents. To update, pass the commit_sha you read as base_commit."
        ),
    )(tool_guard("write_note", deps)(write_note))
