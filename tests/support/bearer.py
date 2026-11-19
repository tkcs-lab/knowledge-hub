"""A test ``CallerResolver``: tokens are ``test:<sub>`` and map to registered callers."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from knowledge_hub.auth.claims import Caller
from knowledge_hub.config import KhConfig

__all__ = ["TestBearerResolver", "rpc_body"]

__test__ = False  # not a pytest class


class TestBearerResolver:
    __test__ = False

    def __init__(self, config: KhConfig) -> None:
        self.config = config
        self._callers: dict[str, Caller] = {}

    def register(self, caller: Caller) -> str:
        known = self._callers.get(caller.sub)
        if known is not None and known != caller:
            raise ValueError(f"a different caller is already registered for sub {caller.sub!r}")
        self._callers[caller.sub] = caller
        return f"test:{caller.sub}"

    async def resolve(self, token: str) -> tuple[Caller | None, str]:
        sub = token.removeprefix("test:") if token.startswith("test:") else None
        caller = self._callers.get(sub) if sub is not None else None
        if caller is None:
            return None, "bearer_unknown"
        if caller.principal_type == "claude_client":
            caller = replace(caller, audiences=self.config.audiences_for_email(caller.email))
        return caller, ""


def rpc_body(method: str, params: dict[str, Any] | None = None, id: int = 1) -> bytes:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode("utf-8")
