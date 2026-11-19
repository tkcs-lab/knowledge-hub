"""The caller of a request: ``Caller``, the parsed ``Call`` and their accessors (plan 3.2, 3.10)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["AUDIENCE_RE", "Call", "Caller", "KhAuthError", "caller_from_scope"]

AUDIENCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class KhAuthError(Exception):
    """The request has no verified caller."""


@dataclass(frozen=True)
class Caller:
    sub: str
    principal_type: str
    email: str | None
    audiences: frozenset[str]
    token_id: str | None
    tools: frozenset[str] | None = None


@dataclass(frozen=True)
class Call:
    """The ``tools/call`` of the request: tool name and parsed ``arguments``."""

    name: str
    arguments: dict[str, Any]


def caller_from_scope(scope: dict[str, Any]) -> Caller:
    caller = (scope.get("state") or {}).get("kh_caller")
    if not isinstance(caller, Caller):
        raise KhAuthError("no verified caller")
    return caller
