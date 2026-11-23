"""The fixed-text catalogue (plan 3.8) and ``tool_guard`` (plan 3.2).

Every text a caller can see from a failed tool is one of the constants below. The catalogue grows with
each tool: a tool adds the texts it needs here.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from psycopg import AsyncConnection

from knowledge_hub.auth.claims import Call, Caller
from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.metrics import METRICS
from knowledge_hub.usage import UsageEvent, write_events

if TYPE_CHECKING:
    from knowledge_hub.mcp.server import Deps

log = logging.getLogger("knowledge_hub.tools")

NOT_FOUND = "not found"
INTERNAL = "internal error (ref {ref})"
RATE_LIMIT = "rate limit exceeded; retry in {n} seconds"
CURSOR_FOREIGN = "cursor does not belong to this query"
NORMATIVE = "only T3 records match; repeat with min_tier T3 to see them"
NORMATIVE_T0A = "only T0a documents or T3 records match; repeat with min_tier T0a or T3 to see them"
BAD_ID = "id must be doc:<uuid> or rec:<uuid>"
BAD_GET_ID = "id must be doc:<uuid>, rec:<uuid> or row:<dataset>:<uuid>"
BAD_LABEL = "unknown label"
BAD_ARG = "argument {name} is invalid"
AUDIENCE_NOT_HELD = "audience is not held by this caller or not configured"
TOO_LARGE = "body is too large"
BAD_TEXT = "text contains forbidden characters"
PATH_EXISTS = "path already exists; pass base_blob to edit it"
BLOB_CHANGED = "the file changed since base_blob"
ID_IN_USE = "id is used by another file in this repo"
TOO_MANY_OPEN = "too many open proposals in this repo"
NOT_CONFIGURED = "writes are not configured"
PROPOSE_FAILED = "could not open the pull request"
ONLY_WORKFLOWS = "this tool is available to workflows only"
MOVE_WRONG_GROUP = "the item is not in from_group"
MOVE_CONFLICT = "the target group already has an item with this source and source_ref, path or id"
MOVE_LINKED = "another item names this one as its replacement or successor; clear that first"
RECORD_MOVED = "a record with this source and source_ref was moved out of this audience"
MOVE_TEXT = "this document cannot be moved by the tool; move it by hand"
TOOL_NOT_ALLOWED = "this credential may not call this tool"
DATASET_RECORD = "this record mirrors a dataset row; change the row instead"
UNKNOWN_DATASET = "unknown dataset"
ROW_INVALID = "row {index}: {problem}"
TOO_MANY_ROWS = "at most 500 rows or keys per call"
SYNC_SHRINK = (
    "finish_sync would mark {n} of {live} rows outdated, more than {percent} percent; "
    "repeat with force=true if that is right"
)
QUERY_TIMEOUT = "query took too long; narrow it with where or limit"
MOVE_PARTIAL = "the removal pull request could not be opened; run kh move again to open it"


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
    events: list[UsageEvent] = field(default_factory=list)


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
            if caller.tools is not None and name not in caller.tools:
                METRICS.tool_call(name, "error", 0.0)
                raise ToolError(TOOL_NOT_ALLOWED)
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
            events: list[UsageEvent] = []
            try:
                async with caller_tx(deps.app_pool, caller) as conn:
                    result = await fn(ToolCall(conn, caller, deps, events), **kwargs)
            except KhError as exc:
                outcome = "error"
                await write_events(deps, caller, events)
                raise ToolError(exc.text) from None
            except ToolError:
                outcome = "error"
                raise
            except Exception:
                outcome = "internal"
                ref = uuid.uuid4()
                log.exception("tool %s failed ref=%s", name, ref)
                raise ToolError(INTERNAL.format(ref=ref)) from None
            else:
                await write_events(deps, caller, events)
                return result
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
