"""``list_groups`` and ``list_repos``: which groups and repos exist (read-only, not limited by group)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

from knowledge_hub.config import KhConfig
from knowledge_hub.directory.model import Membership
from knowledge_hub.mcp.errors import ToolCall, tool_guard
from knowledge_hub.mcp.server import Deps

log = logging.getLogger(__name__)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def build_groups(config: KhConfig, membership: Membership) -> dict[str, Any]:
    emails = {login.casefold(): email for login, email in membership.people.items()}
    groups = []
    for name in sorted(config.active_groups):
        group = config.groups[name]
        logins = sorted(membership.approvers.get(name, frozenset()))
        groups.append(
            {
                "name": name,
                "repo": config.repo_for_group(name),
                "slack_channel": group.slack_channel,
                "approvers": [{"login": login, "email": emails.get(login.casefold())} for login in logins],
                "approver_directory_groups": [
                    {"provider": provider, "group": ref}
                    for provider in sorted(group.directory)
                    for ref in group.directory[provider].approvers
                ],
            }
        )
    return {"groups": groups}


def build_repos(
    config: KhConfig,
    state: dict[str, tuple[str | None, str | None]] | None,
    counts: dict[str, int] | None,
) -> dict[str, Any]:
    have_index = state is not None and counts is not None

    def index_fields(repo: str) -> dict[str, Any]:
        if not have_index:
            return {"documents": None, "last_indexed_sha": None, "last_indexed_at": None}
        sha, at = state.get(repo, (None, None))  # type: ignore[union-attr]
        return {"documents": counts.get(repo, 0), "last_indexed_sha": sha, "last_indexed_at": at}  # type: ignore[union-attr]

    repos: list[dict[str, Any]] = []
    for name in sorted(config.active_groups):
        repo = config.repo_for_group(name)
        repos.append(
            {"name": repo, "kind": "group", "audience": name, "dirs": None, "labels": None}
            | index_fields(repo)
        )
    for repo in config.tech_repo_names:
        repos.append(
            {
                "name": repo,
                "kind": "tech",
                "audience": config.tech_audience(repo),
                "dirs": config.tech_dirs(repo),
                "labels": config.visible_labels(config.tech_labels(repo)),
            }
            | index_fields(repo)
        )
    return {"tech_org": config.tech_org, "repos": repos}


async def read_index_state(pool: Any) -> tuple[dict[str, tuple[str | None, str | None]], dict[str, int]]:
    """``(state, counts)``: the same two reads as the panel, on the indexer pool."""
    async with pool.connection() as conn:
        await conn.execute("SET TRANSACTION READ ONLY")
        try:
            rows = await (
                await conn.execute("SELECT repo, last_indexed_sha, indexed_at FROM kh.repo_state")
            ).fetchall()
            count_rows = await (
                await conn.execute("SELECT repo, count(*) FROM kh.documents GROUP BY repo")
            ).fetchall()
        finally:
            await conn.rollback()
    return {repo: (sha, _iso(at)) for repo, sha, at in rows}, {repo: n for repo, n in count_rows}


async def list_groups(call: ToolCall) -> dict[str, Any]:
    deps = call.deps
    membership = deps.membership.current() if deps.membership is not None else None
    if membership is None:
        membership = Membership.from_config(deps.config)
    return build_groups(deps.config, membership)


async def list_repos(call: ToolCall) -> dict[str, Any]:
    deps = call.deps
    state = counts = None
    if deps.indexer_pool is not None:
        try:
            state, counts = await read_index_state(deps.indexer_pool)
        except Exception:
            log.exception("list_repos: reading the index state failed")
            state = counts = None
    return build_repos(deps.config, state, counts)


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="list_groups",
        description=(
            "List the groups with their repo, Slack channel and approvers (login and email) and the "
            "directory groups the approvers come from. Any signed-in caller may use it."
        ),
    )(tool_guard("list_groups", deps)(list_groups))
    server.tool(
        name="list_repos",
        description=(
            "List the indexed repos with their kind (group or tech), the audience that reads them, the "
            "directories and labels of tech repos and the index state (documents, last indexed commit "
            "and time). Any signed-in caller may use it."
        ),
    )(tool_guard("list_repos", deps)(list_repos))
