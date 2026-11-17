"""The one definition of a counted approval."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from knowledge_hub.proposals import NO_REQUESTER, PROPOSE_REF_PREFIX, REQUESTER_CONTEXT, REQUESTER_POSTERS

BOT_LOGINS: tuple[str, ...] = ("kh-hub-bot",)


def current_approvals(
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    bot_logins: Collection[str],
    exclude_logins: Collection[str] = (),
) -> list[dict[str, Any]]:
    head = (pr.get("head") or {}).get("sha")
    author = (pr.get("user") or {}).get("login")
    author_fold = author.casefold() if isinstance(author, str) else None
    bots = {b.casefold() for b in bot_logins}
    excluded = {e.casefold() for e in exclude_logins}
    out = []
    for r in reviews:
        login = (r.get("user") or {}).get("login")
        if (
            r.get("state") == "APPROVED"
            and r.get("official")
            and not r.get("dismissed")
            and head is not None
            and r.get("commit_id") == head
            and isinstance(login, str)
            and login.casefold() != author_fold
            and login.casefold() not in bots
            and login.casefold() not in excluded
        ):
            out.append(r)
    return out


def is_proposal(pr: dict[str, Any]) -> bool:
    return ((pr.get("head") or {}).get("ref") or "").startswith(PROPOSE_REF_PREFIX)


def requester_from_statuses(
    pr: dict[str, Any], statuses: list[dict[str, Any]]
) -> tuple[tuple[str, ...], str | None]:
    """Requester logins recorded by ``kh-hub-bot`` for the head commit, and a problem if any.

    A proposal PR needs exactly one valid record bound to its head ref; any other PR yields every
    login named by a valid record (the uuid is not checked) and never a problem.
    """
    proposal = is_proposal(pr)
    suffix = ((pr.get("head") or {}).get("ref") or "")[len(PROPOSE_REF_PREFIX) :] if proposal else None
    logins: dict[str, str] = {}
    for s in statuses:
        if (
            s.get("context") != REQUESTER_CONTEXT
            or s.get("state") != "success"
            or s.get("creator") not in REQUESTER_POSTERS
        ):
            continue
        parts = (s.get("description") or "").split()
        if len(parts) != 2:
            continue
        login, uuid = parts
        if proposal and uuid != suffix:
            continue
        logins.setdefault(login.casefold(), login)
    found = tuple(sorted(logins.values()))
    if proposal:
        return (found, None) if len(found) == 1 else ((), NO_REQUESTER)
    return found, None
