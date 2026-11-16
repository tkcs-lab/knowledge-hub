"""Pure visibility rules shared by derived records, candidates and conflicts (plan 3.7, 3.8).

The SQL in ``0005_derived_visibility.sql`` applies the same rules; this module is the Python
statement of them, with no database access.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import kh_error

if TYPE_CHECKING:
    from knowledge_hub.config import Config


CLAUSE_SEP = "|"


def clause(audiences: Iterable[str]) -> str:
    """Write ``audiences`` as one clause: the names sorted, de-duplicated and joined by ``|``."""
    names = sorted(set(audiences))
    if not names:
        raise ValueError("a clause needs at least one audience")
    return CLAUSE_SEP.join(names)


def clause_members(value: str) -> frozenset[str]:
    """Return the audience names of one clause."""
    return frozenset(value.split(CLAUSE_SEP))


@dataclass(frozen=True)
class Visibility:
    """The visibility columns of a row.

    ``required_audiences``: a list of clauses (``finance|mgmt``; a plain name is a one-member
    clause). A caller reads the row when every clause shares at least one audience with the
    caller's. ``allowed_emails``: ``None`` means no email restriction; a list means only those
    (lower-case) emails; ``[]`` means nobody.
    """

    required_audiences: list[str]
    allowed_emails: list[str] | None = None


def derive_visibility(sources: list[Visibility]) -> Visibility:
    """Return the visibility of a row derived from ``sources``.

    The union of the sources' clauses, which is an AND over the sources (the caller must
    satisfy every clause of every source), and the intersection of the non-null email lists:
    ``None`` when every source has none, ``[]`` when the intersection is empty. Both lists come
    back sorted and de-duplicated. A derived row needs at least one source, so an empty list is
    an error.
    """
    if not sources:
        raise ValueError("a derived row needs at least one source")
    audiences = sorted({a for s in sources for a in s.required_audiences})
    email_lists = [set(s.allowed_emails) for s in sources if s.allowed_emails is not None]
    if not email_lists:
        return Visibility(required_audiences=audiences, allowed_emails=None)
    return Visibility(required_audiences=audiences, allowed_emails=sorted(set.intersection(*email_lists)))


def can_read(vis: Visibility, audiences: Iterable[str], email: str | None) -> bool:
    """Python statement of ``kh.visible`` (tests only): every clause meets the caller's audiences."""
    held = set(audiences)
    if not all(clause_members(c) & held for c in vis.required_audiences):
        return False
    return vis.allowed_emails is None or (email is not None and email in vis.allowed_emails)


def route_target_repo(sources: list[Visibility], config: Config, explicit_repo: str | None = None) -> str:
    """Pick the canonical repo a promotion may be written to, never widening visibility (plan 3.5).

    Any source restricted to named people is refused. ``K`` is the set of the sources' clauses;
    with ``restricted_audiences_imply_all`` a clause containing ``all`` becomes ``{all}``, which is
    dropped when ``K`` has another clause. ``R``, the intersection of ``K``'s clauses, holds the
    audiences whose members can read every source. With ``explicit_repo`` it is accepted when ``K``
    is empty or its audience is in ``R``. Without: empty ``K`` goes to the T0 repo for ``all``, an
    empty ``R`` is refused as spanning, and the T0 repos whose audience is in ``R`` must be exactly
    one (none is ``no_repo``, several is ``ambiguous``).
    """
    if any(s.allowed_emails is not None for s in sources):
        raise kh_error("email_sources", errors.EMAIL_SOURCES)
    clauses = {clause_members(c) for s in sources for c in s.required_audiences}
    if config.restricted_audiences_imply_all:
        clauses = {frozenset({"all"}) if "all" in c else c for c in clauses}
        if len(clauses) > 1:
            clauses.discard(frozenset({"all"}))
    rendered = sorted(clause(c) for c in clauses)
    common: frozenset[str] | None = frozenset.intersection(*clauses) if clauses else None

    if explicit_repo is not None:
        repo = config.repos.get(explicit_repo)
        if repo is None:
            raise kh_error("no_repo", errors.NO_REPO, a=explicit_repo)
        if common is not None and repo.audience not in common:
            raise kh_error("wider", errors.WIDER, repo=explicit_repo, a=repo.audience, u=", ".join(rendered))
        return explicit_repo

    if common is None:
        wanted = {"all"}
    elif not common:
        raise kh_error("span", errors.SPAN, a=rendered[0], b=rendered[1])
    else:
        wanted = set(common)
    matches = sorted(n for n, r in config.repos.items() if r.tier == "T0" and r.audience in wanted)
    if len(matches) >= 2:
        raise kh_error("ambiguous", errors.AMBIGUOUS, repo_a=matches[0], repo_b=matches[1])
    if matches:
        return matches[0]
    raise kh_error("no_repo", errors.NO_REPO, a=" or ".join(sorted(wanted)))
