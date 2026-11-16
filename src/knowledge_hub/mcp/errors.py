"""The fixed-text catalogue (plan 3.9) and ``tool_guard`` (plan 3.2).

Every text a caller can see from a failed tool is one of the constants below. Later phases import
them and never add to them, except ``RATE_LIMIT`` (phase p23).
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from psycopg import AsyncConnection

from knowledge_hub.auth.claims import Call, Caller
from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.metrics import METRICS

if TYPE_CHECKING:
    from knowledge_hub.mcp.server import Deps

log = logging.getLogger("knowledge_hub.tools")

NOT_FOUND = "not found"
INTERNAL = "internal error (ref {ref})"
CURSOR_FOREIGN = "cursor does not belong to this query"
NORMATIVE = (
    "cannot answer normatively from T2/T3 only: {n} lower-tier hits; "
    'call search with min_tier="T3" to see them'
)
VERSION_NOT_INDEXED = "version {sha} is not an indexed version of this document"
INGEST_FORBIDDEN = "ingest_raw is limited to workflow principals"
INGEST_TOO_LARGE = "body too large (limit {n} bytes)"
INGEST_NO_MAP = "cannot ingest: no audience mapping for series or folder and no participants"
INGEST_AUDIENCE = "cannot ingest: your token does not hold the audience this series or folder maps to"
ALREADY_INGESTED = "already ingested: {id}"
BAD_ID = "malformed id {x}"
BAD_KIND = "kind must be one of summary, extracted-fact, decision-candidate, changelog"
BAD_CLAIMS = "claims_json must be a JSON list of objects with text, source_ids and a quote or timestamp"
BAD_VERSION = "@version is only allowed on doc: ids"
TOO_MANY_SOURCES = "at most 50 sources"
VERSION_CHANGED = "source {id} changed since you read it (now {version})"
DERIVED_SOURCES = "cannot write derived record: {k} cited sources are not found or not visible to you"
EMAIL_SOURCES = (
    "cannot propose: a cited source is restricted to named people; no canonical repo is that narrow"
)
SPAN = "cannot propose: sources span audiences {a}, {b}; no canonical repo is restricted to all of them"
AMBIGUOUS = (
    "cannot propose: the sources fit both {repo_a} and {repo_b}; "
    "pass target_id of a document in the repository to use"
)
NO_REPO = "cannot propose: no canonical repository is configured for audience {a}"
WIDER = "cannot propose to {repo}: its audience {a} is wider than or different from the sources' audience {u}"
BAD_PATH = (
    "new_path must be a new markdown file under portfolio/, product/ or technical/ in the given domain "
    "(lowercase letters, digits, - _ /)"
)
DUPLICATE = "an identical proposal is already open: {id} (PR #{n})"
LIMIT = "daily promotion limit for domain {d} in {repo} reached ({n}); try again after {time} UTC"
BAD_CANDIDATE = "candidate_id must name one of your own failed proposals"
PR_FAILED = "could not open the pull request; call propose_change again with candidate_id={id} to retry"
RATE_LIMIT = "rate limit exceeded; retry in {n} seconds"


class KhError(Exception):
    """A failure with a fixed, caller-safe text."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(code)
        self.code = code
        self.text = text


def kh_error(code: str, template: str, **fields: Any) -> KhError:
    return KhError(code, template.format(**fields))


@dataclass(frozen=True)
class ToolCall:
    """What a guarded tool function receives first: the open caller transaction and its context."""

    conn: AsyncConnection
    caller: Caller
    deps: Deps


def _same(a: Any, b: Any) -> bool:
    """Equality with ints and floats compared by value (JSON does not tell ``1`` from ``1.0``)."""
    num = (int, float)
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, num) and isinstance(b, num):
        return float(a) == float(b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _refuse(why: str) -> ToolError:
    ref = uuid.uuid4()
    log.warning("tool call refused (%s) ref=%s", why, ref)
    return ToolError(INTERNAL.format(ref=ref))


_Tool = Callable[..., Awaitable[Any]]


def tool_guard(name: str, deps: Deps) -> Callable[[_Tool], _Tool]:
    """Wrap ``fn(call: ToolCall, **tool_arguments)`` as the SDK-facing tool ``name``.

    The wrapper takes the SDK's ``Context`` first, reads the verified caller and the call the claim
    was bound to from the request scope, refuses any call whose dispatched arguments are not exactly
    the hashed ones, opens ``caller_tx`` and maps failures to fixed texts.
    """

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())[1:]
        defaults = {p.name: p.default for p in params if p.default is not inspect.Parameter.empty}

        @functools.wraps(fn)
        async def wrapper(ctx: Context, **kwargs: Any) -> Any:
            request = ctx.request_context.request
            state = (request.scope.get("state") if request is not None else None) or {}
            caller = state.get("kh_caller")
            call = state.get("kh_call")
            if not isinstance(caller, Caller):
                raise _refuse("no verified caller")
            if not isinstance(call, Call):
                raise _refuse("not a tools/call")
            if call.name != name:
                raise _refuse("tool name differs from the hashed call")
            expected = {**defaults, **call.arguments}
            if not _same(kwargs, expected):
                raise _refuse("arguments differ from the hashed call")
            limiter = deps.ratelimiter
            if limiter is not None:
                allowed, retry = limiter.allow(caller.sub, time.monotonic())
                if not allowed:
                    METRICS.tool_call(name, "rate_limited", 0.0)
                    raise ToolError(RATE_LIMIT.format(n=retry))
            started = time.monotonic()
            outcome = "ok"
            try:
                async with caller_tx(deps.app_pool, caller) as conn:
                    return await fn(ToolCall(conn, caller, deps), **kwargs)
            except KhError as exc:
                outcome = "error"
                raise ToolError(exc.text) from None
            except ToolError:
                outcome = "error"
                raise
            except Exception:
                outcome = "internal"
                ref = uuid.uuid4()
                log.exception("tool %s failed ref=%s", name, ref)
                raise ToolError(INTERNAL.format(ref=ref)) from None
            finally:
                METRICS.tool_call(name, outcome, time.monotonic() - started)

        wrapper.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [
                inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context),
                *params,
            ],
            return_annotation=sig.return_annotation,
        )
        wrapper.__annotations__ = {
            "ctx": Context,
            **{p.name: p.annotation for p in params},
            "return": sig.return_annotation,
        }
        return wrapper

    return decorate
