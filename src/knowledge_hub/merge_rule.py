"""The pure self-merge rule shared by the trust walk and ``kh domain-owners``."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from knowledge_hub.governance.owners import OwnersFile

REVIEWED = "reviewed"
OWNER_MERGE = "owner_merge"
ANCHOR = "anchor"
NOT_ENABLED = "self-merge is not enabled"
NO_MERGER = "the merger is unknown"
OTHER_MERGER = "merged by someone other than the author"
BOT_AUTHOR = "the author is a bot"
NO_OWNERS = "owners.yml is not available at the parent commit"
NO_PATHS = "no changed paths"
NOT_OWNER = "the author does not own every changed path through a self-merge team"

_GHOST = "ghost"


@dataclass(frozen=True)
class SelfMerge:
    ok: bool
    reason: str | None
    uncovered: tuple[str, ...] = ()


def touched_paths(changes: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Sorted unique ``filename`` and ``previous_filename`` of the changed files."""
    paths: set[str] = set()
    for change in changes:
        for key in ("filename", "previous_filename"):
            value = change.get(key)
            if value:
                paths.add(value)
    return tuple(sorted(paths))


def teams_to_check(
    owners: OwnersFile | None, paths: Sequence[str], self_merge_teams: Collection[str]
) -> frozenset[str] | None:
    """Teams whose membership of the author must be read; ``None`` when the check cannot pass."""
    if owners is None or not paths:
        return frozenset()
    teams: set[str] = set()
    for path in paths:
        rule = owners.rule_for(path)
        if rule is None or rule.team not in self_merge_teams:
            return None
        teams.add(rule.team)
    return frozenset(teams)


def _id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _is_ghost(login: str | None) -> bool:
    return login is not None and login.casefold() == _GHOST


def same_person(
    author: str | None,
    merger: str | None,
    author_id: int | None = None,
    merger_id: int | None = None,
) -> bool:
    if not author or not merger or _is_ghost(author) or _is_ghost(merger):
        return False
    a, m = _id(author_id), _id(merger_id)
    if a is not None and m is not None:
        return a == m
    return author.casefold() == merger.casefold()


def is_bot(login: str | None, bots: Collection[str]) -> bool:
    if not login:
        return False
    folded = login.casefold()
    return any(folded == b.casefold() for b in bots)


def self_merge_check(
    *,
    author: str | None,
    merger: str | None,
    author_id: int | None = None,
    merger_id: int | None = None,
    bots: Collection[str],
    owners: OwnersFile | None,
    paths: Sequence[str],
    self_merge_teams: Collection[str],
    author_teams: Collection[str],
) -> SelfMerge:
    if not self_merge_teams:
        return SelfMerge(False, NOT_ENABLED)
    if not author or not merger:
        return SelfMerge(False, NO_MERGER)
    if _is_ghost(author) or _is_ghost(merger):
        return SelfMerge(False, NO_MERGER)
    if not same_person(author, merger, author_id, merger_id):
        return SelfMerge(False, OTHER_MERGER)
    if is_bot(author, bots):
        return SelfMerge(False, BOT_AUTHOR)
    if owners is None:
        return SelfMerge(False, NO_OWNERS)
    if not paths:
        return SelfMerge(False, NO_PATHS)
    uncovered: list[str] = []
    for path in paths:
        rule = owners.rule_for(path)
        if rule is None or rule.team not in self_merge_teams or rule.team not in author_teams:
            uncovered.append(path)
    if uncovered:
        return SelfMerge(False, NOT_OWNER, tuple(sorted(uncovered)))
    return SelfMerge(True, None)
