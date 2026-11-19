"""Answer 404 on the public host for every path that is not public (plan 3.10)."""

from __future__ import annotations

from typing import Any

__all__ = ["PublicHostGuard"]

def normalize_host(value: str) -> str:
    """Lower-cased hostname without port, ``[v6]`` brackets or trailing dot."""
    host = value.strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


PREFIXES = ("/.well-known/", "/oauth/")


class PublicHostGuard:
    def __init__(self, app: Any, public_host: str, public_paths: frozenset[str] | set[str]) -> None:
        self.app = app
        self.public_host = normalize_host(public_host)
        self.public_paths = frozenset(public_paths)

    def _public(self, path: str) -> bool:
        if path in self.public_paths:
            return True
        return path.startswith(PREFIXES) and ".." not in path.split("/")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        hidden = (
            scope["type"] == "http"
            and self._is_public_host(scope)
            and not self._public(scope["path"])
        )
        if hidden:
            await send(
                {"type": "http.response.start", "status": 404, "headers": [(b"content-length", b"0")]}
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)

    def _is_public_host(self, scope: dict[str, Any]) -> bool:
        host = self._host(scope)
        # Fail closed: a request with no (or an empty) Host header cannot be told apart
        # from one for the public name, so it gets the public-name treatment.
        return host is None or host == "" or host == self.public_host

    @staticmethod
    def _host(scope: dict[str, Any]) -> str | None:
        for name, value in scope.get("headers", []):
            if name.lower() == b"host":
                return normalize_host(value.decode("latin-1"))
        return None
