"""Run the app in process and call tools with bearer tokens from the test resolver (plan 3.10)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from knowledge_hub.auth.claims import Caller
from tests.support.bearer import TestBearerResolver, rpc_body

HOST = "hub.test.example:8080"
BASE_URL = f"http://{HOST}"
PROTOCOL_VERSION = "2025-06-18"


@dataclass(frozen=True)
class ToolResult:
    status: int
    is_error: bool
    text: str
    data: Any = None


class McpClient:
    def __init__(self, http: httpx.AsyncClient, resolver: TestBearerResolver) -> None:
        self.http = http
        self.resolver = resolver

    def _headers(self, token: str | None) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": PROTOCOL_VERSION,
        }
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        return headers

    async def post(self, body: bytes, token: str | None) -> httpx.Response:
        return await self.http.post("/mcp", content=body, headers=self._headers(token))

    async def call(
        self,
        caller: Caller,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        handshake: bool = True,
    ) -> ToolResult:
        """Handshake, then ``tools/call`` with the registered token of ``caller`` (or ``token``)."""
        args = arguments if arguments is not None else {}
        registered = self.resolver.register(caller)
        if token is None:
            token = registered
        if handshake:
            init = rpc_body(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
                id=0,
            )
            first = await self.post(init, token)
            if first.status_code != 200:
                return ToolResult(first.status_code, True, first.text)
            note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
            await self.post(note, token)
        body = rpc_body("tools/call", {"name": tool, "arguments": args}, id=1)
        return self._result(await self.post(body, token))

    @staticmethod
    def _result(resp: httpx.Response) -> ToolResult:
        if resp.status_code != 200:
            return ToolResult(resp.status_code, True, resp.text)
        message = resp.json()
        if "error" in message:
            return ToolResult(200, True, message["error"].get("message", ""))
        result = message["result"]
        text = "".join(c.get("text", "") for c in result.get("content", []))
        data = result.get("structuredContent")
        return ToolResult(200, bool(result.get("isError")), text, data)
