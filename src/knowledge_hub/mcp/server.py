"""``build_mcp``: the MCP server and its tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from psycopg_pool import AsyncConnectionPool

from knowledge_hub.config import Config
from knowledge_hub.settings import Settings


@dataclass
class Deps:
    settings: Settings
    config: Config
    app_pool: AsyncConnectionPool
    indexer_pool: AsyncConnectionPool | None
    embedder: Any = None
    blobs: Any = None
    forgejo: Any = None
    grist: Any = None
    verifier: Any = None
    ratelimiter: Any = None
    worker: Any = None


def register_tools(server: MCPServer, deps: Deps) -> None:
    """Register every tool. Later phases add their modules here."""
    from knowledge_hub.tools import docs, raw, search

    docs.register(server, deps)
    search.register(server, deps)
    raw.register(server, deps)


def build_mcp(deps: Deps) -> MCPServer:
    server = MCPServer("knowledge-hub")
    register_tools(server, deps)
    return server
