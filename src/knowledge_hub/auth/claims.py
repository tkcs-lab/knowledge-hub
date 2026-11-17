"""Caller verification for ``/mcp``: PrivacyFence identity claims as a pure ASGI middleware."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import anyio.to_thread

from knowledge_hub.auth.pf_claim_verify import (
    CLAIM_HEADER,
    ClaimError,
    ClaimVerifier,
    InMemoryReplayCache,
    Principal,
)
from knowledge_hub.config import KhConfig
from knowledge_hub.metrics import METRICS
from knowledge_hub.settings import Settings

__all__ = [
    "AUDIENCE_RE",
    "Call",
    "Caller",
    "ClaimMiddleware",
    "InMemoryReplayCache",
    "KhAuthError",
    "ReplayCache",
    "caller_from_scope",
    "resolve_audiences",
]

log = logging.getLogger("knowledge_hub.auth")

AUDIENCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MCP_PATHS = frozenset({"/mcp", "/mcp/"})
REPLAY_LEEWAY_SECONDS = 5.0
_HEADER_NAME = CLAIM_HEADER.lower().encode("ascii")


class KhAuthError(Exception):
    """The request has no verified caller."""


@dataclass(frozen=True)
class Caller:
    sub: str
    principal_type: str
    email: str | None
    audiences: frozenset[str]
    token_id: str | None


@dataclass(frozen=True)
class Call:
    """The ``tools/call`` the claim was verified against: tool name and parsed ``arguments``."""

    name: str
    arguments: dict[str, Any]


class ReplayCache(Protocol):
    def add_if_new(self, jti: str, expires_at: float) -> bool: ...


def caller_from_scope(scope: dict[str, Any]) -> Caller:
    caller = (scope.get("state") or {}).get("kh_caller")
    if not isinstance(caller, Caller):
        raise KhAuthError("no verified caller")
    return caller


class _Refuse(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _parse_call(body: bytes) -> Call | None:
    """The tool call in a request body, or ``None`` (the verifier already refused bad bodies)."""
    if not body.strip():
        return None
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise _Refuse(401, "bad_body") from exc
    if not isinstance(parsed, dict):
        raise _Refuse(401, "bad_body")
    if parsed.get("method") != "tools/call":
        return None
    params = parsed.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        raise _Refuse(401, "bad_body")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise _Refuse(401, "bad_body")
    return Call(params["name"], arguments)


def resolve_audiences(
    config: KhConfig, principal_type: str, email: str | None, claim_audiences: Iterable[str]
) -> frozenset[str]:
    """The groups a caller holds: from the verified email for people, from the claim for workflows."""
    if principal_type == "claude_client":
        return config.audiences_for_email(email)
    if principal_type == "n8n_workflow":
        return frozenset(a for a in claim_audiences if a in config.groups)
    return frozenset()


def _build_caller(
    sub: str,
    principal_type: str,
    email: str | None,
    audiences: Any,
    token_id: str | None,
    call: Call | None,
    *,
    config: KhConfig,
) -> Caller:
    if principal_type == "human":
        raise _Refuse(401, "human_refused")
    if principal_type == "service" and call is not None:
        raise _Refuse(401, "service_tool_call")
    if any(not isinstance(a, str) or not AUDIENCE_RE.fullmatch(a) for a in audiences):
        raise _Refuse(401, "bad_audience_name")
    kept_email = email.lower() if principal_type == "claude_client" and email else None
    return Caller(
        sub,
        principal_type,
        kept_email,
        resolve_audiences(config, principal_type, kept_email, audiences),
        token_id,
    )


class ClaimMiddleware:
    def __init__(
        self,
        app: Any,
        verifier: ClaimVerifier,
        settings: Settings,
        replay_cache: ReplayCache | None,
        *,
        config: KhConfig,
    ) -> None:
        if settings.env == "prod" and settings.dev_identity is not None:
            raise ValueError("KH_DEV_IDENTITY is set while KH_ENV=prod; refusing to start")
        self.app = app
        self.verifier = verifier
        self.settings = settings
        self.replay_cache = replay_cache
        self.config = config

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] not in MCP_PATHS:
            await self.app(scope, receive, send)
            return
        try:
            await self._handle(scope, receive, send)
        except _Refuse as exc:
            if exc.status == 401:
                log.warning("claim refused: %s", exc.code)
                METRICS.claim_rejection(exc.code)
            await self._empty(send, exc.status)

    async def _handle(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        method = scope["method"]
        if method not in ("POST", "GET", "DELETE"):
            await self._empty(send, 405, allow=True)
            return
        body = await self._read_body(scope, receive) if method == "POST" else b""
        token = self._header(scope)
        if token is None:
            if self.settings.dev_identity is None:
                raise _Refuse(401, "missing_header")
            call = _parse_call(body)
            caller = self._dev_caller(call)
            log.warning("no claim header; using the dev identity")
        else:
            caller, call = await self._verified(token, body)
        if method != "POST":
            await self._empty(send, 405, allow=True)
            return
        state = scope.setdefault("state", {})
        state["kh_caller"] = caller
        state["kh_call"] = call
        await self.app(scope, self._replay(body, receive), send)

    @staticmethod
    def _header(scope: dict[str, Any]) -> str | None:
        for name, value in scope["headers"]:
            if name.lower() == _HEADER_NAME:
                return value.decode("latin-1")
        return None

    async def _read_body(self, scope: dict[str, Any], receive: Any) -> bytes:
        limit = self.settings.max_body_bytes
        for name, value in scope["headers"]:
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    raise _Refuse(401, "bad_content_length") from None
                if declared > limit:
                    raise _Refuse(413, "too_large")
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                raise _Refuse(401, "disconnected")
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                raise _Refuse(413, "too_large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    async def _verified(self, token: str, body: bytes) -> tuple[Caller, Call | None]:
        try:
            principal: Principal = await anyio.to_thread.run_sync(
                lambda: self.verifier.verify(token, body=body)
            )
        except ClaimError as exc:
            raise _Refuse(401, exc.code) from exc
        call = _parse_call(body)
        caller = _build_caller(
            principal.sub,
            principal.principal_type,
            principal.email,
            principal.audiences,
            principal.token_id,
            call,
            config=self.config,
        )
        if self.replay_cache is not None:
            cache = self.replay_cache
            fresh = await anyio.to_thread.run_sync(
                lambda: cache.add_if_new(principal.jti, principal.expires_at + REPLAY_LEEWAY_SECONDS)
            )
            if not fresh:
                raise _Refuse(401, "replayed")
        return caller, call

    def _dev_caller(self, call: Call | None) -> Caller:
        ident = self.settings.dev_identity
        assert ident is not None
        return _build_caller(
            ident.sub, ident.principal_type, ident.email, ident.audiences, None, call, config=self.config
        )

    @staticmethod
    def _replay(body: bytes, receive: Any) -> Any:
        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return replay

    @staticmethod
    async def _empty(send: Any, status: int, allow: bool = False) -> None:
        headers = [(b"content-length", b"0")]
        if allow:
            headers.append((b"allow", b"POST"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})
