"""Pure resolution of ``supersedes`` links into ``superseded_by`` (plan section 3.2)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class DocLink:
    id: str  # documents.id as text
    doc_key: str
    path: str
    supersedes: tuple[str, ...]
    current: str | None  # documents.superseded_by as text, or None


@dataclass(frozen=True)
class Resolution:
    links: dict[str, str | None]  # every DocLink.id -> successor id or None
    alerts: list[tuple[str, str]]  # (path, message)


def resolve(rows: Sequence[DocLink]) -> Resolution:
    by_key = {r.doc_key: r for r in rows}
    by_id = {r.id: r for r in rows}
    alerts: list[tuple[str, str]] = []
    successors: dict[str, set[str]] = {}  # target id -> ids of documents naming it
    for new in rows:
        for key in new.supersedes:
            target = by_key.get(key)
            if target is None:
                alerts.append((new.path, f"supersedes unknown id {key}"))
            elif target.id != new.id:
                successors.setdefault(target.id, set()).add(new.id)

    chosen: dict[str, str] = {}
    for target_id, names in successors.items():
        best = min(names, key=lambda i: by_id[i].doc_key)
        chosen[target_id] = best
        if len(names) > 1:
            alerts.append(
                (by_id[target_id].path, f"superseded by {len(names)} documents; using {by_id[best].doc_key}")
            )

    links: dict[str, str | None] = {r.id: None for r in rows}
    for target_id, succ_id in chosen.items():
        cycle = _cycle(target_id, chosen)
        if cycle is None:
            links[target_id] = succ_id
        else:
            keys = ", ".join(sorted(by_id[i].doc_key for i in cycle))
            alerts.append((by_id[target_id].path, f"supersedes cycle: {keys}"))
    return Resolution(links=links, alerts=sorted(set(alerts)))


def _cycle(start: str, chosen: dict[str, str]) -> set[str] | None:
    """The ids of the cycle through ``start``, or None when ``start`` is not on one."""
    seen = [start]
    node = chosen.get(start)
    while node is not None and node != start and node not in seen:
        seen.append(node)
        node = chosen.get(node)
    return set(seen) if node == start else None
