"""The one definition of a counted approval."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

BOT_LOGINS: tuple[str, ...] = ("kh-hub-bot",)


def current_approvals(
    pr: dict[str, Any], reviews: list[dict[str, Any]], bot_logins: Collection[str]
) -> list[dict[str, Any]]:
    head = (pr.get("head") or {}).get("sha")
    author = (pr.get("user") or {}).get("login")
    out = []
    for r in reviews:
        login = (r.get("user") or {}).get("login")
        if (
            r.get("state") == "APPROVED"
            and r.get("official")
            and not r.get("dismissed")
            and head is not None
            and r.get("commit_id") == head
            and login is not None
            and login != author
            and login not in bot_logins
        ):
            out.append(r)
    return out
