"""Pure approval check for ``kh domain-owners`` (plan 3.8)."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from knowledge_hub.governance.owners import OwnersFile, glob_to_regex


@dataclass(frozen=True)
class OwnersProblem:
    pattern: str
    team: str
    text: str


def _logins(counted: Sequence[Mapping[str, Any]]) -> set[str]:
    out: set[str] = set()
    for review in counted:
        login = (review.get("user") or {}).get("login")
        if isinstance(login, str):
            out.add(login)
    return out


def check_owners(
    *,
    owners: OwnersFile,
    paths: Sequence[str],
    counted: Sequence[Mapping[str, Any]],
    members: Mapping[str, Collection[str]],
    widen: tuple[str, Collection[str]] | None,
    widen_reason_ok: bool,
    widen_label_problem: str | None,
) -> list[str]:
    """Output lines of the unsatisfied requirements, sorted and de-duplicated; empty means pass."""
    reviewers = _logins(counted)

    def approved_by(team: str) -> bool:
        return bool(reviewers & set(members.get(team, ())))

    lines: set[str] = set()
    matched: dict[int, tuple[Any, list[str]]] = {}
    for path in sorted(set(paths)):
        rule = owners.rule_for(path)
        if rule is None:
            lines.add(f"{path}: no owner in owners.yml")
            continue
        matched.setdefault(id(rule), (rule, []))[1].append(path)
    problems: list[OwnersProblem] = []
    for rule, touched in matched.values():
        if approved_by(rule.team):
            continue
        pattern = next(p for p in rule.paths if any(glob_to_regex(p).fullmatch(t) for t in touched))
        problems.append(OwnersProblem(pattern, rule.team, f"{pattern}: approval from {rule.team} required"))
    lines.update(p.text for p in problems)

    if widen_label_problem is not None:
        lines.add(widen_label_problem)
    if widen is not None:
        _audience, teams = widen
        if not any(approved_by(t) for t in teams):
            lines.add(f"widen-audience needs approval from one of: {', '.join(sorted(teams))}")
        if not widen_reason_ok:
            lines.add("widen-audience needs a Reason for widening line")
    return sorted(lines)
