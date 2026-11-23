"""``list_datasets``, ``query`` and ``save_query`` (plan 3.6, the p7 part)."""

from __future__ import annotations

import json
from typing import Any

import psycopg.errors
from mcp.server.mcpserver import MCPServer
from psycopg.types.json import Jsonb

from knowledge_hub.datasets.catalog import load_catalog, load_dataset
from knowledge_hub.datasets.model import SAVED_RE, Dataset, to_json_value
from knowledge_hub.datasets.query import QuerySpec, build_query, parse_spec
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import ToolCall, kh_error, tool_guard
from knowledge_hub.mcp.server import Deps

_SPEC_KEYS = ("select", "where", "group_by", "metrics", "time_bucket", "order", "limit", "include_outdated")
_ROWS_CAP = 100001


def _held(call: ToolCall) -> set[str]:
    return {a for a in call.caller.audiences if call.deps.config.is_active(a)}


def _require_held(call: ToolCall, audience: str) -> None:
    if audience not in _held(call):
        raise kh_error("audience_not_held", errors.AUDIENCE_NOT_HELD)


def _bad(name: str) -> Exception:
    return kh_error("bad_arg", errors.BAD_ARG, name=name)


def _iso(value: Any) -> str:
    return value.isoformat() if value is not None else ""


async def _describe(call: ToolCall, ds: Dataset, held: list[str], with_rows: bool) -> dict[str, Any]:
    from psycopg import sql

    table = sql.Identifier("kh_data", ds.name)
    out: dict[str, Any] = {
        "name": ds.name,
        "title": ds.title,
        "description": ds.description,
        "title_column": ds.title_column,
        "audiences": held,
        "history": ds.history,
        "columns": [
            {
                "name": c.name,
                "label": c.label,
                "type": c.type,
                "choices": list(c.choices),
                "ref_dataset": c.ref_dataset,
                "source_name": c.source_name,
                "filterable": c.filterable,
                "embed": c.embed,
                "sensitive": c.sensitive,
                "indexed": c.indexed,
            }
            for c in ds.columns
        ],
    }
    async with call.conn.cursor() as cur:
        await cur.execute(
            sql.SQL("SELECT max(_source_updated_at) FROM {} WHERE _audience = ANY(%s)").format(table),
            (held,),
        )
        out["watermark"] = _iso((await cur.fetchone())[0])  # type: ignore[index]
        await cur.execute(
            "SELECT DISTINCT ON (audience) audience, sync_id, finished_at, watermark"
            " FROM kh.dataset_syncs WHERE dataset = %s AND audience = ANY(%s) AND finished_at IS NOT NULL"
            " ORDER BY audience, finished_at DESC",
            (ds.name, held),
        )
        out["last_syncs"] = [
            {"audience": a, "sync_id": s, "finished_at": _iso(f), "watermark": _iso(w)}
            for a, s, f, w in await cur.fetchall()
        ]
        if with_rows:
            await cur.execute(
                sql.SQL(
                    "SELECT count(*) FROM (SELECT 1 FROM {} WHERE _audience = ANY(%s) AND NOT _outdated"
                    " LIMIT {}) s"
                ).format(table, sql.Literal(_ROWS_CAP)),
                (held,),
            )
            n = (await cur.fetchone())[0]  # type: ignore[index]
            out["rows"] = min(n, _ROWS_CAP - 1)
            out["rows_capped"] = n >= _ROWS_CAP
    return out


async def list_datasets(call: ToolCall, dataset: str = "") -> dict[str, Any]:
    held = _held(call)
    catalog = [ds for ds in await load_catalog(call.conn) if held & set(ds.audiences)]
    if dataset:
        catalog = [ds for ds in catalog if ds.name == dataset]
        if not catalog:
            raise kh_error("unknown_dataset", errors.UNKNOWN_DATASET)
    datasets = [
        await _describe(call, ds, sorted(held & set(ds.audiences)), bool(dataset)) for ds in catalog
    ]
    names = [d["name"] for d in datasets]
    async with call.conn.cursor() as cur:
        await cur.execute(
            "SELECT audience, name, dataset FROM kh.saved_queries"
            " WHERE audience = ANY(%s) AND dataset = ANY(%s) ORDER BY audience, name",
            (sorted(held), names),
        )
        saved = [{"audience": a, "name": n, "dataset": d} for a, n, d in await cur.fetchall()]
    return {"datasets": datasets, "saved_queries": saved}


async def _visible(call: ToolCall, name: str, audience: str | None) -> tuple[Dataset, list[str]]:
    ds = await load_dataset(call.conn, name) if name else None
    audiences = [audience] if audience else sorted(_held(call))
    audiences = [a for a in audiences if ds is not None and a in ds.audiences]
    if ds is None or not audiences:
        raise kh_error("unknown_dataset", errors.UNKNOWN_DATASET)
    return ds, audiences


def _parse(call: ToolCall, ds: Dataset, spec: dict[str, Any], as_of: str, error_arg: str | None) -> QuerySpec:
    try:
        return parse_spec(
            ds, **{k: spec[k] for k in _SPEC_KEYS}, as_of=as_of, max_rows=call.deps.settings.query_max_rows
        )
    except errors.KhError:
        if error_arg is None:
            raise
        raise _bad(error_arg) from None


async def query(
    call: ToolCall,
    dataset: str = "",
    select: list[str] = [],  # noqa: B006
    where: str = "",
    group_by: list[str] = [],  # noqa: B006
    metrics: list[str] = [],  # noqa: B006
    time_bucket: str = "",
    order: list[str] = [],  # noqa: B006
    limit: int = 100,
    include_outdated: bool = False,
    as_of: str = "",
    saved: str = "",
    audience: str = "",
) -> dict[str, Any]:
    if audience:
        _require_held(call, audience)
    if not saved:
        if not dataset:
            raise _bad("dataset")
        ds, audiences = await _visible(call, dataset, audience or None)
        args = dict(
            select=select, where=where, group_by=group_by, metrics=metrics, time_bucket=time_bucket,
            order=order, limit=limit, include_outdated=include_outdated,
        )  # fmt: skip
        spec = _parse(call, ds, args, as_of, None)
    else:
        if not audience:
            raise _bad("audience")
        if (dataset, select, where, group_by, metrics, time_bucket, order, include_outdated) != (
            "", [], "", [], [], "", [], False,
        ):  # fmt: skip
            raise _bad("saved")
        async with call.conn.cursor() as cur:
            await cur.execute(
                "SELECT dataset, spec FROM kh.saved_queries WHERE audience = %s AND name = %s",
                (audience, saved),
            )
            found = await cur.fetchone()
        if found is None:
            raise kh_error("not_found", errors.NOT_FOUND)
        ds, audiences = await _visible(call, found[0], audience)
        stored = dict(found[1])
        args = {k: stored[k] for k in _SPEC_KEYS}
        if limit != 100:
            args["limit"] = limit
        spec = _parse(call, ds, args, as_of, "saved")
    statement, params, columns = build_query(spec, ds, audiences)
    try:
        async with call.conn.transaction():
            ms = int(call.deps.settings.query_timeout_ms)
            await call.conn.execute(f"SET LOCAL statement_timeout = '{ms}ms'")
            async with call.conn.cursor() as cur:
                await cur.execute(statement, params)
                got = await cur.fetchall()
    except psycopg.errors.QueryCanceled:
        raise kh_error("query_timeout", errors.QUERY_TIMEOUT) from None
    return {
        "dataset": ds.name,
        "columns": columns,
        "rows": [[to_json_value(v) for v in r] for r in got[: spec.limit]],
        "truncated": len(got) > spec.limit,
    }


async def save_query(
    call: ToolCall,
    name: str,
    audience: str,
    dataset: str,
    select: list[str] = [],  # noqa: B006
    where: str = "",
    group_by: list[str] = [],  # noqa: B006
    metrics: list[str] = [],  # noqa: B006
    time_bucket: str = "",
    order: list[str] = [],  # noqa: B006
    limit: int = 100,
    include_outdated: bool = False,
) -> dict[str, Any]:
    if not SAVED_RE.fullmatch(name):
        raise _bad("name")
    _require_held(call, audience)
    ds, audiences = await _visible(call, dataset, audience)
    args = dict(
        select=select, where=where, group_by=group_by, metrics=metrics, time_bucket=time_bucket,
        order=order, limit=limit, include_outdated=include_outdated,
    )  # fmt: skip
    spec = _parse(call, ds, args, "", None)
    build_query(spec, ds, audiences)
    stored = {"dataset": dataset, **args}
    await call.conn.execute(
        "INSERT INTO kh.saved_queries (audience, name, dataset, spec, created_by_sub)"
        " VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (audience, name) DO UPDATE SET dataset = EXCLUDED.dataset, spec = EXCLUDED.spec,"
        " created_by_sub = EXCLUDED.created_by_sub, updated_at = now()",
        (audience, name, dataset, Jsonb(json.loads(json.dumps(stored))), call.caller.sub),
    )
    return {"audience": audience, "name": name}


def register(server: MCPServer, deps: Deps) -> None:
    server.tool(
        name="list_datasets",
        description=(
            "List the datasets you may see with their columns, the newest source_updated_at (watermark) "
            "and the last finished syncs; name one dataset to also get its row count."
        ),
    )(tool_guard("list_datasets", deps)(list_datasets))
    server.tool(
        name="query",
        description=(
            "Read a dataset for a table or chart: select, where (JSON list of {column, op, value}), "
            "group_by, metrics (count, sum:col, avg:col, min:col, max:col, count_distinct:col), "
            "time_bucket (col:month), order (-name for descending) and limit. Names come from "
            "list_datasets. saved runs a saved query of audience."
        ),
    )(tool_guard("query", deps)(query))
    server.tool(
        name="save_query",
        description=(
            "Save a query under a name for one group you hold; it always reads only that group's rows. "
            "Saving the same name again replaces it."
        ),
    )(tool_guard("save_query", deps)(save_query))
