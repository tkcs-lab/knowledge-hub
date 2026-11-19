"""Directory data classes and the membership view (plan 58, 3.4). Pure, no I/O."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from knowledge_hub.config import KhConfig

APPROVER_NOT_MEMBER = "approver is not a member"


@dataclass(frozen=True)
class DirectoryUser:
    # No defaults: a missing flag is a parse error, never a pass.
    email: str
    enabled: bool
    email_verified: bool
    username: str = ""


@dataclass(frozen=True)
class GroupFetch:
    members: tuple[DirectoryUser, ...]
    approvers: tuple[DirectoryUser, ...]


@dataclass(frozen=True)
class Skip:
    group: str
    role: str  # "member" or "approver"
    who: str
    reason: str


@dataclass(frozen=True)
class Membership:
    people: Mapping[str, str]  # login -> lower email
    members: Mapping[str, frozenset[str]]  # group -> logins
    approvers: Mapping[str, frozenset[str]]
    skips: tuple[Skip, ...] = ()

    def audiences_for_email(self, email: str | None, is_active: Callable[[str], bool]) -> frozenset[str]:
        if not email:
            return frozenset()
        low = email.lower()
        logins = {login.casefold() for login, e in self.people.items() if e == low}
        if not logins:
            return frozenset()
        return frozenset(
            group
            for group, members in self.members.items()
            if is_active(group) and logins & {m.casefold() for m in members}
        )

    @staticmethod
    def from_config(config: KhConfig) -> Membership:
        """Static membership only; the approver-must-be-a-member rule applies here too."""
        members = {name: frozenset(g.members) for name, g in config.groups.items()}
        approvers, skips = resolve_approvers(
            {name: frozenset(g.approvers) for name, g in config.groups.items()}, members, config.people
        )
        return Membership(dict(config.people), members, approvers, tuple(sorted(set(skips), key=_skip_key)))


class MembershipView(Protocol):
    def current(self) -> Membership: ...


def _skip_key(skip: Skip) -> tuple[str, str, str, str]:
    return (skip.group, skip.role, skip.who, skip.reason)


def resolve_approvers(
    wanted: Mapping[str, frozenset[str]],
    members: Mapping[str, frozenset[str]],
    people: Mapping[str, str],
) -> tuple[dict[str, frozenset[str]], list[Skip]]:
    """Keep the wanted approvers that are final members; record the others as skips."""
    approvers: dict[str, frozenset[str]] = {}
    skips: list[Skip] = []
    for group, logins in wanted.items():
        folded = {m.casefold() for m in members.get(group, frozenset())}
        kept = {login for login in logins if login.casefold() in folded}
        approvers[group] = frozenset(kept)
        for login in sorted(logins - kept):
            skips.append(Skip(group, "approver", people.get(login, login), APPROVER_NOT_MEMBER))
    return approvers, skips
