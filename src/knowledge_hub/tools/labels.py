"""``list_labels``: the labels that documents and records may carry (plan 3.8)."""

from typing import Any

from mcp.server.mcpserver import MCPServer
from psycopg.rows import dict_row

from knowledge_hub.mcp.errors import ToolCall, tool_guard
from knowledge_hub.mcp.server import Deps


async def list_labels(call: ToolCall) -> list[dict[str, Any]]:
    async with call.conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT name, description FROM kh.labels ORDER BY name")
        rows = await cur.fetchall()
    archived = call.deps.config.archived_label_set
    return [r for r in rows if r["name"] not in archived]


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="list_labels",
        description="List the labels (name and description) that search accepts and documents may carry.",
    )(tool_guard("list_labels", deps)(list_labels))
