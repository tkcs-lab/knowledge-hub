"""Caller verification for ``/mcp``: bearer credentials (or the dev identity) as a pure ASGI middleware."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from knowledge_hub.auth.claims import Call, Caller
from knowledge_hub.config import KhConfig
from knowledge_hub.metrics import METRICS
from knowledge_hub.settings import Settings

__all__ = ["CallerMiddleware", "CallerResolver"]

log = logging.getLogger("knowledge_hub.auth")

MCP_PATHS = frozenset({"/mcp", "/mcp/"})
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"


class CallerResolver(Protocol):
    async def resolve(self, token: str) -> tuple[Caller | None, str]:
        """The caller of a bearer token, or ``(None, <rejection code>)``."""
        ...


class _Refuse(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _parse_call(body: bytes) -> Call | None:
    """The tool call in a request body, or ``None`` when the body is not a ``tools/call``."""
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


class CallerMiddleware:
    def __init__(
        self,
        app: Any,
        resolver: CallerResolver | None,
        settings: Settings,
        *,
        config: KhConfig,
        public_url: str | None = None,
    ) -> None:
        if settings.env == "prod" and settings.dev_identity is not None:
            raise ValueError("KH_DEV_IDENTITY is set while KH_ENV=prod; refusing to start")
        self.app = app
        self.resolver = resolver
        self.settings = settings
        self.config = config
        self.public_url = public_url.rstrip("/") if public_url else None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] not in MCP_PATHS:
            await self.app(scope, receive, send)
            return
        try:
            await self._handle(scope, receive, send)
        except _Refuse as exc:
            if exc.status == 401:
                log.warning("auth refused: %s", exc.code)
                METRICS.auth_rejection(exc.code)
                await self._empty(send, 401, www=self._www_authenticate(exc.code != "missing_credentials"))
                return
            await self._empty(send, exc.status)

    def _www_authenticate(self, token_sent: bool) -> str:
        if self.public_url:
            value = f'Bearer resource_metadata="{self.public_url}{RESOURCE_METADATA_PATH}"'
            return value + (', error="invalid_token"' if token_sent else "")
        return 'Bearer error="invalid_token"' if token_sent else "Bearer"

    @staticmethod
    def _authorization(scope: dict[str, Any]) -> str | None:
        for name, value in scope["headers"]:
            if name.lower() == b"authorization":
                return value.decode("latin-1")
        return None

    @staticmethod
    def _bearer_token(header: str) -> str | None:
        scheme, sep, token = header.partition(" ")
        if scheme.lower() != "bearer" or not sep or not token or token != token.strip() or " " in token:
            return None
        return token

    async def _handle(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        method = scope["method"]
        if method not in ("POST", "GET", "DELETE"):
            await self._empty(send, 405, allow=True)
            return
        header = self._authorization(scope)
        caller: Caller
        if header is not None:
            token = self._bearer_token(header)
            if token is None:
                raise _Refuse(401, "bearer_unknown")
            if self.resolver is None:
                raise _Refuse(401, "bearer_unknown")
            resolved, code = await self.resolver.resolve(token)
            if resolved is None:
                raise _Refuse(401, code or "bearer_unknown")
            caller = resolved
        elif self.settings.dev_identity is not None:
            caller = self._dev_caller()
            log.warning("no credentials; using the dev identity")
        else:
            raise _Refuse(401, "missing_credentials")
        if method != "POST":
            await self._empty(send, 405, allow=True)
            return
        body = await self._read_body(scope, receive)
        call = _parse_call(body)
        state = scope.setdefault("state", {})
        state["kh_caller"] = caller
        state["kh_call"] = call
        await self.app(scope, self._replay(body, receive), send)

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

    def _dev_caller(self) -> Caller:
        ident = self.settings.dev_identity
        assert ident is not None
        if ident.principal_type == "claude_client":
            email = ident.email.lower() if ident.email else None
            audiences = self.config.audiences_for_email(email)
        else:
            email = None
            audiences = frozenset(a for a in ident.audiences if self.config.is_active(a))
        return Caller(ident.sub, ident.principal_type, email, audiences, None, None)

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
    async def _empty(send: Any, status: int, allow: bool = False, www: str | None = None) -> None:
        headers = [(b"content-length", b"0")]
        if allow:
            headers.append((b"allow", b"POST"))
        if www is not None:
            headers.append((b"www-authenticate", www.encode("latin-1")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})
