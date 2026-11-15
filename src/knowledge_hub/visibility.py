"""Pure visibility rules shared by derived records, candidates and conflicts (plan 3.7, 3.8).

The SQL in ``0005_derived_visibility.sql`` applies the same rules; this module is the Python
statement of them, with no database access.
"""

from __future__ import annotations

from dataclasses import dataclass


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
