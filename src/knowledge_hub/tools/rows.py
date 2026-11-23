"""``upsert_rows``, ``delete_rows`` and ``finish_sync``: hub keys sync a dataset (plan 3.3)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from knowledge_hub.datasets import rows as rowlib
from knowledge_hub.datasets.catalog import load_dataset
from knowledge_hub.datasets.model import SYNC_RE, Dataset
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps


async def _dataset(call: ToolCall, dataset: str, audience: str, sync_id: str, need_sync: bool) -> Dataset:
    if call.caller.principal_type != "n8n_workflow":
        raise kh_error("only_workflows", errors.ONLY_WORKFLOWS)
    if audience not in call.caller.audiences or not call.deps.config.is_active(audience):
        raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD)
    ds = await load_dataset(call.conn, dataset)
    if ds is None or audience not in ds.audiences:
        raise kh_error("unknown_dataset", errors.UNKNOWN_DATASET)
    if (sync_id == "" and need_sync) or (sync_id != "" and not SYNC_RE.fullmatch(sync_id)):
        raise kh_error("bad_arg", errors.BAD_ARG, name="sync_id")
    return ds


async def upsert_rows(
    call: ToolCall, dataset: str, audience: str, rows: str, sync_id: str = ""
) -> dict[str, Any]:
    ds = await _dataset(call, dataset, audience, sync_id, False)
    try:
        parsed = rowlib.parse_rows(rows, ds)
    except rowlib.RowError as exc:
        raise kh_error("row_invalid", errors.ROW_INVALID, index=exc.index, problem=exc.problem) from None
    return await rowlib.upsert(call.conn, ds, audience, parsed, sync_id)


async def delete_rows(call: ToolCall, dataset: str, audience: str, keys: list[str]) -> dict[str, Any]:
    ds = await _dataset(call, dataset, audience, "", False)
    if not keys:
        raise kh_error("bad_arg", errors.BAD_ARG, name="keys")
    if len(keys) > rowlib.MAX_ROWS:
        raise kh_error("too_many_rows", errors.TOO_MANY_ROWS)
    if any(not isinstance(k, str) or not 1 <= len(k) <= 200 or "\x00" in k for k in keys):
        raise kh_error("bad_arg", errors.BAD_ARG, name="keys")
    return await rowlib.delete(call.conn, ds, audience, keys)


async def finish_sync(
    call: ToolCall, dataset: str, audience: str, sync_id: str, force: bool = False
) -> dict[str, Any]:
    ds = await _dataset(call, dataset, audience, sync_id, True)
    return await rowlib.finish(call.conn, ds, audience, sync_id, force)


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="upsert_rows",
        description=(
            "Insert or update up to 500 rows of a dataset for the key's group (hub keys only). "
            'rows is a JSON array of {"key", "source_updated_at", "fields"}; fields are named by '
            "column or source_name. A row with an older source_updated_at than the stored one leaves "
            "the stored values. Pass sync_id during a full sync and call finish_sync at the end."
        ),
    )(tool_guard("upsert_rows", deps)(upsert_rows))
    server.tool(
        name="delete_rows",
        description=(
            "Mark rows of a dataset outdated by key (hub keys only); they leave search and query results."
        ),
    )(tool_guard("delete_rows", deps)(delete_rows))
    server.tool(
        name="finish_sync",
        description=(
            "End a full sync: rows of this group not sent since the sync started are marked outdated "
            "(hub keys only). Refused when it would outdate more than the dataset's max_outdated_percent "
            "of live rows unless force is true."
        ),
    )(tool_guard("finish_sync", deps)(finish_sync))
