"""Pure membership rules (plan 58, 3.4): classify directory users and merge them over the static config."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence

from knowledge_hub.config import KhConfig
from knowledge_hub.directory.model import DirectoryUser, GroupFetch, Membership, Skip, resolve_approvers

LOCAL_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,38}")
SHARED_LOGIN = "login shared by several emails"
RESERVED_LOGIN = "owners"


def login_from_email(email: str) -> str | None:
    low = email.strip().lower()
    if low.count("@") != 1:
        return None
    local = low.split("@", 1)[0]
    return local if LOCAL_RE.fullmatch(local) else None


def classify(user: DirectoryUser, domains: Collection[str], reserved: Collection[str]) -> str:
    """The first matching reason to skip the user, or "" to keep it."""
    if not user.enabled:
        return "disabled"
    if not user.email_verified:
        return "email not verified"
    email = user.email.strip().lower()
    if email.count("@") != 1:
        return "no valid email"
    if email.split("@", 1)[1] not in {d.lower() for d in domains}:
        return "domain not allowed"
    login = login_from_email(email)
    if login is None:
        return "not a valid login"
    if login.casefold() in {r.casefold() for r in reserved}:
        return "bot or reserved login"
    return ""


def effective(
    config: KhConfig, fetched: Mapping[str, GroupFetch] | None, domains: Sequence[str]
) -> Membership:
    if fetched is None:
        return Membership.from_config(config)
    reserved = {config.bot_login, config.indexer_login, RESERVED_LOGIN}
    static_login = {login.casefold(): login for login in config.people}
    static_email = {email: login for login, email in config.people.items()}

    skips: list[Skip] = []
    passing: list[tuple[str, str, str]] = []  # (group, role, email)
    for group in config.active_groups:
        data = fetched.get(group)
        if data is None:
            continue
        for role, users in (("member", data.members), ("approver", data.approvers)):
            for user in users:
                email = user.email.strip().lower()
                reason = classify(user, domains, reserved)
                if reason:
                    skips.append(Skip(group, role, email, reason))
                else:
                    passing.append((group, role, email))

    # Resolve one login per passing email.
    emails_of_login: dict[str, set[str]] = {}
    login_of: dict[str, str] = {}
    for _, _, email in passing:
        if email in login_of:
            continue
        if email in static_email:
            login_of[email] = static_email[email]
            continue
        login = login_from_email(email) or ""
        login_of[email] = login
        emails_of_login.setdefault(login, set()).add(email)
    refused: set[str] = set()
    for login, emails in emails_of_login.items():
        if login.casefold() in static_login or len(emails) > 1:
            refused |= emails

    people = dict(config.people)
    members = {name: set(g.members) for name, g in config.groups.items()}
    wanted = {name: set(g.approvers) for name, g in config.groups.items()}
    for group, role, email in passing:
        if email in refused:
            skips.append(Skip(group, role, email, SHARED_LOGIN))
            continue
        login = login_of[email]
        people.setdefault(login, email)
        (members if role == "member" else wanted)[group].add(login)

    final_members = {name: frozenset(v) for name, v in members.items()}
    approvers, approver_skips = resolve_approvers(
        {n: frozenset(v) for n, v in wanted.items()}, final_members, people
    )
    skips.extend(approver_skips)
    ordered = tuple(sorted(set(skips), key=lambda s: (s.group, s.role, s.who, s.reason)))
    return Membership(people, final_members, approvers, ordered)
