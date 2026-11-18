"""``build_mcp``: the MCP server and its tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from psycopg_pool import AsyncConnectionPool

from knowledge_hub.config import KhConfig
from knowledge_hub.settings import Settings


@dataclass
class Deps:
    settings: Settings
    config: KhConfig
    app_pool: AsyncConnectionPool
    indexer_pool: AsyncConnectionPool | None
    embedder: Any = None
    blobs: Any = None
    verifier: Any = None
    ratelimiter: Any = None
    reader: Any = None
    worker: Any = None
    forgejo_bot: Any = None
    tech_reader: Any = None


def register_tools(server: MCPServer, deps: Deps) -> None:
    """Register every tool; later phases add their modules here."""
    from knowledge_hub.tools import docs, labels, move, outdated, propose, records, search, usage

    search.register(server, deps)
    docs.register(server, deps)
    labels.register(server, deps)
    records.register(server, deps)
    outdated.register(server, deps)
    move.register(server, deps)
    propose.register(server, deps)
    usage.register(server, deps)


def build_mcp(deps: Deps) -> MCPServer:
    server = MCPServer("knowledge-hub")
    register_tools(server, deps)
    return server
