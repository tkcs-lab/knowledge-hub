"""Run the app in process and call tools the way PrivacyFence does (plan 3.2).

PrivacyFence opens a fresh MCP session per call: ``initialize``, ``notifications/initialized`` and
``tools/call``, each request with its own signed claim; only the ``tools/call`` claim carries the
caller's audiences and the ``req`` hash of the call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from knowledge_hub.auth.claims import Caller
from knowledge_hub.auth.pf_claim_verify import CLAIM_HEADER
from tests.support.claims import mint_claim, rpc_body

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
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    def _headers(self, token: str | None) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": PROTOCOL_VERSION,
        }
        if token is not None:
            headers[CLAIM_HEADER] = token
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
        """Handshake, then ``tools/call`` signed for exactly ``arguments`` (or with ``token``)."""
        args = arguments if arguments is not None else {}
        who = {"sub": caller.sub, "principal_type": caller.principal_type, "email": caller.email}
        if handshake:
            init = rpc_body(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "privacyfence", "version": "0"},
                },
                id=0,
            )
            first = await self.post(init, mint_claim(**who, audiences=[]))
            assert first.status_code == 200, first.text
            note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
            await self.post(note, mint_claim(**who, audiences=[]))
        body = rpc_body("tools/call", {"name": tool, "arguments": args}, id=1)
        if token is None:
            token = mint_claim(**who, audiences=sorted(caller.audiences), tool=tool, arguments=args)
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
