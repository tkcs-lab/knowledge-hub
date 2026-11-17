"""``propose_edit``: open a pull request that edits an existing canonical document (plan 16, 3.2 to 3.4).

The new file starts from the current ``main`` version: ``id``, ``domain``, ``type``, ``owner``,
``status``, ``supersedes`` and ``superseded_by`` are kept; title, ``review_by``, entities and body are
replaced. ``submit`` is shared with ``propose_doc``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

from knowledge_hub.agent import texts
from knowledge_hub.agent.guards import check_citations, require_audience, require_requester
from knowledge_hub.agent.render import render_file, verify_roundtrip
from knowledge_hub.agent.rules import (
    _normalise,
    check_body,
    check_entities,
    check_rationale,
    check_review_by,
    check_title,
)
from knowledge_hub.forgejo.client import ForgejoError
from knowledge_hub.governance.rules import parse_layout
from knowledge_hub.indexer.frontmatter import Rejected, parse_document
from knowledge_hub.mcp.errors import KhError, ToolCall, tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools import promote
from knowledge_hub.tools.agent_proposals import submit


def _not_editable() -> KhError:
    return KhError("not_editable", texts.NOT_EDITABLE)


async def propose_edit(
    call: ToolCall,
    target_id: str,
    title: str,
    review_by: str,
    body: str,
    rationale: str,
    entities: list[str] = [],  # noqa: B006 - never mutated
) -> dict[str, Any]:
    deps, config, settings = call.deps, call.deps.config, call.deps.settings
    if not settings.agent_proposals_enabled:
        raise KhError("disabled_proposals", texts.DISABLED_PROPOSALS)
    bot = deps.forgejo_bot
    if bot is None or deps.forgejo is None:
        raise KhError("no_forgejo", promote.NO_FORGEJO)
    requester = await require_requester(call, deps.forgejo)
    try:
        _, repo, path = await promote._target_doc(call.conn, target_id)
    except KhError as exc:
        if exc.code != "bad_path":  # a visible row outside the domain folders (a note, glossary.md)
            raise
        raise _not_editable() from None
    spec = config.repos.get(repo)
    if spec is None or spec.tier != "T0":
        raise _not_editable()
    require_audience(call.caller, spec.audience)
    layout = parse_layout(path, config.domains)
    if layout is None or layout.is_adr or layout.is_glossary:
        raise _not_editable()

    try:
        head = await bot.head_sha(repo, "main")
    except ForgejoError as exc:
        raise promote._internal("head_sha", exc) from None
    try:
        raw = await bot.get_raw(repo, path, head)
    except ForgejoError:
        raise _not_editable() from None
    current = parse_document(raw, path, config.domains)
    if isinstance(current, Rejected):
        raise _not_editable()

    title = check_title(title)
    due = check_review_by(review_by, datetime.now(UTC).date())
    body = check_body(body, settings.agent_max_body_bytes)
    rationale = check_rationale(rationale)
    refs = check_entities(entities, config, spec.audience)
    await check_citations(call.conn, config, repo, title, body, rationale)

    kept: dict[str, Any] = {
        "id": current.id,
        "domain": current.domain,
        "type": current.type,
        "owner": current.owner,
        "status": current.status,
        "supersedes": current.supersedes,
        "superseded_by": current.superseded_by,
    }
    text = render_file(title=title, review_by=due, entities=refs, body=body, **kept)
    try:
        verify_roundtrip(text, path, config, title=title, review_by=due, entities=refs, body=body, **kept)
    except ValueError as exc:
        raise promote._internal("render", exc) from None
    if (
        current.title == title
        and current.review_by == due
        and list(current.entities) == list(refs)
        and _normalise(current.body) == body
    ):
        raise KhError("no_change", texts.NO_CHANGE)
    return await submit(
        call,
        tool="propose_edit",
        kind="edit",
        repo=repo,
        path=path,
        doc_key=current.id,
        domain=current.domain,
        title=title,
        subject=f"Edit {path}",
        text=text,
        rationale=rationale,
        requester=requester,
        base_sha=head,
    )


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="propose_edit",
        description=(
            "Propose an edit to an existing canonical document as a pull request for human review. "
            "Give the doc: id and the full new body; the document starts from its current main version."
        ),
    )(tool_guard("propose_edit", deps)(propose_edit))
