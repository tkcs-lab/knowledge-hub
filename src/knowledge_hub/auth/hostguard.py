"""Answer 404 on the public host for every path that is not public (plan 3.10)."""

from __future__ import annotations

from typing import Any

__all__ = ["PublicHostGuard"]

PREFIXES = ("/.well-known/", "/oauth/")


class PublicHostGuard:
    def __init__(self, app: Any, public_host: str, public_paths: frozenset[str] | set[str]) -> None:
        self.app = app
        self.public_host = public_host.lower()
        self.public_paths = frozenset(public_paths)

    def _public(self, path: str) -> bool:
        if path in self.public_paths:
            return True
        return path.startswith(PREFIXES) and ".." not in path.split("/")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        hidden = (
            scope["type"] == "http"
            and self._host(scope) == self.public_host
            and not self._public(scope["path"])
        )
        if hidden:
            await send(
                {"type": "http.response.start", "status": 404, "headers": [(b"content-length", b"0")]}
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _host(scope: dict[str, Any]) -> str:
        for name, value in scope.get("headers", []):
            if name.lower() == b"host":
                return value.decode("latin-1").strip().lower()
        return ""
