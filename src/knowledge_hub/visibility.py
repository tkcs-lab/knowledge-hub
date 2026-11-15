"""Pure visibility rules shared by derived records, candidates and conflicts (plan 3.7, 3.8).

The SQL in ``0005_derived_visibility.sql`` applies the same rules; this module is the Python
statement of them, with no database access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import kh_error

if TYPE_CHECKING:
    from knowledge_hub.config import Config


@dataclass(frozen=True)
class Visibility:
    """The visibility columns of a row.

    ``required_audiences``: every listed audience must be held. ``allowed_emails``: ``None``
    means no email restriction; a list means only those (lower-case) emails; ``[]`` means
    nobody.
    """

    required_audiences: list[str]
    allowed_emails: list[str] | None = None


def derive_visibility(sources: list[Visibility]) -> Visibility:
    """Return the visibility of a row derived from ``sources``.

    The union of the audiences and the intersection of the non-null email lists: ``None``
    when every source has none, ``[]`` when the intersection is empty. Both lists come back
    sorted and de-duplicated. A derived row needs at least one source, so an empty list is
    an error.
    """
    if not sources:
        raise ValueError("a derived row needs at least one source")
    audiences = sorted({a for s in sources for a in s.required_audiences})
    email_lists = [set(s.allowed_emails) for s in sources if s.allowed_emails is not None]
    if not email_lists:
        return Visibility(required_audiences=audiences, allowed_emails=None)
    return Visibility(required_audiences=audiences, allowed_emails=sorted(set.intersection(*email_lists)))


def route_target_repo(sources: list[Visibility], config: Config, explicit_repo: str | None = None) -> str:
    """Pick the canonical repo a promotion may be written to, never widening visibility (plan 3.8).

    Any source restricted to named people is refused. ``U`` is the union of the sources'
    audiences, minus ``all`` when ``restricted_audiences_imply_all`` is set and ``U`` has other
    members. Without ``explicit_repo``: empty ``U`` goes to the T0 repo for ``all``, ``{a}`` to the
    T0 repo for ``a`` and two or more audiences are refused. With ``explicit_repo`` it is accepted
    only when ``U`` is a subset of that repo's audience.
    """
    if any(s.allowed_emails is not None for s in sources):
        raise kh_error("email_sources", errors.EMAIL_SOURCES)
    union = {a for s in sources for a in s.required_audiences}
    if config.restricted_audiences_imply_all and len(union) > 1:
        union.discard("all")
    ordered = sorted(union)

    if explicit_repo is not None:
        repo = config.repos.get(explicit_repo)
        if repo is None:
            raise kh_error("no_repo", errors.NO_REPO, a=explicit_repo)
        if not union <= {repo.audience}:
            raise kh_error("wider", errors.WIDER, repo=explicit_repo, a=repo.audience, u=", ".join(ordered))
        return explicit_repo

    if len(ordered) >= 2:
        raise kh_error("span", errors.SPAN, a=ordered[0], b=ordered[1])
    audience = ordered[0] if ordered else "all"
    for name, repo in config.repos.items():
        if repo.tier == "T0" and repo.audience == audience:
            return name
    raise kh_error("no_repo", errors.NO_REPO, a=audience)
