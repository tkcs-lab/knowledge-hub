"""Guards shared by the agent write tools (plan sections 3.3, 3.6, 3.12).

Each guard raises ``KhError`` with the exact text of ``agent/texts.py`` or ``mcp/errors.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from knowledge_hub.agent import texts
from knowledge_hub.agent.rules import Requester, cited_ids, requester_from_caller
from knowledge_hub.auth.claims import Caller
from knowledge_hub.config import Config
from knowledge_hub.forgejo.client import ForgejoRefused
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import KhError, ToolCall, kh_error
from knowledge_hub.metrics import METRICS
from knowledge_hub.settings import Settings
from knowledge_hub.visibility import Visibility, route_target_repo

log = logging.getLogger("knowledge_hub.agent")

MAX_CITED = 50


def require_audience(caller: Caller, audience: str) -> None:
    if audience not in caller.audiences:
        raise KhError("audience_not_held", texts.AUDIENCE_NOT_HELD.format(a=audience))


def t1_repo(config: Config, audience: str) -> str:
    for name, repo in config.repos.items():
        if repo.tier == "T1" and repo.audience == audience:
            return name
    raise KhError("no_notes_repo", texts.NO_NOTES_REPO.format(a=audience))


def t0_repo(config: Config, audience: str) -> str:
    for name, repo in config.repos.items():
        if repo.tier == "T0" and repo.audience == audience:
            return name
    raise kh_error("no_repo", errors.NO_REPO, a=audience)


def allowed_domains(settings: Settings) -> list[str]:
    return [d for d in (p.strip().lower() for p in settings.agent_email_domains.split(",")) if d]


async def require_requester(call: ToolCall, client: Any) -> Requester:
    requester = requester_from_caller(call.caller, allowed_domains(call.deps.settings))
    if not await client.org_member(requester.login):
        raise KhError("bad_login", texts.BAD_LOGIN)
    return requester


async def check_citations(conn: Any, config: Config, repo: str, *texts_: str) -> None:
    ids = cited_ids(*texts_)
    if len(ids) > MAX_CITED:
        raise KhError("too_many_sources", errors.TOO_MANY_SOURCES)
    if not ids:
        return
    cur = await conn.execute(
        "SELECT n_resolved, required_audiences, allowed_emails FROM kh.strict_visibility(%s::text[])", (ids,)
    )
    n_resolved, audiences, emails = await cur.fetchone()
    if n_resolved == 0:
        return
    route_target_repo(
        [
            Visibility(
                required_audiences=list(audiences), allowed_emails=None if emails is None else list(emails)
            )
        ],
        config,
        repo,
    )


def record_write_failure(tool: str, exc: BaseException) -> None:
    reason = "refused" if isinstance(exc, ForgejoRefused) else "forgejo_error"
    log.error("agent write failed tool=%s reason=%s", tool, reason)
    METRICS.agent_write_failure(tool, reason)
