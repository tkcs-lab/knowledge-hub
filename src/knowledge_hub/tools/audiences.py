"""``list_audiences``: who holds each group, for PrivacyFence's audiences source (ADR 0325 there)."""

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from knowledge_hub.auth.claims import PF_SERVICE_SUB
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps

log = logging.getLogger("knowledge_hub.tools")


async def list_audiences(call: ToolCall) -> dict[str, Any]:
    if call.caller.principal_type != "service" or call.caller.sub != PF_SERVICE_SUB:
        raise kh_error("service_only", errors.AUDIENCES_SERVICE_ONLY)
    table = call.deps.config.audience_members()
    log.info("list_audiences audiences=%d addresses=%d", len(table), sum(len(v) for v in table.values()))
    return {"version": 1, "audiences": table}


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="list_audiences",
        description=(
            'PrivacyFence only: who holds each group of the hub, as {"version": 1, "audiences": '
            '{group: [email, ...]}}. Every other caller is refused.'
        ),
    )(tool_guard("list_audiences", deps)(list_audiences))
