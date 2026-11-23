"""The structured query builder for datasets (plan 3.6, the p6 part). Pure: no I/O.

Column names reach the statement only as ``psycopg.sql.Identifier`` after a catalog check, and
values only as parameters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from psycopg import sql

from knowledge_hub.datasets.model import Column, Dataset
from knowledge_hub.datasets.rows import coerce
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import KhError, kh_error

__all__ = ["MAX_CONDS", "MAX_IN", "Cond", "QuerySpec", "build_query", "parse_spec"]

MAX_CONDS = 20
MAX_IN = 100
OPS = frozenset({"eq", "ne", "lt", "le", "gt", "ge", "in", "is_null", "not_null"})
ORDERED_OPS = frozenset({"lt", "le", "gt", "ge"})
_SQL_OPS = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
UNITS = frozenset({"day", "week", "month", "quarter", "year"})
METRIC_KINDS = frozenset({"count_distinct", "sum", "avg", "min", "max"})

# The virtual fields next to the user columns; they are never in a dataset's catalog.
_VIRTUAL = {
    "_key": Column("_key", "text", "Key", 0),
    "_audience": Column("_audience", "text", "Audience", 0, filterable=True),
    "_source_updated_at": Column("_source_updated_at", "timestamp", "Source updated", 0, filterable=True),
}


@dataclass(frozen=True)
class Cond:
    column: str
    op: str
    value: Any


@dataclass(frozen=True)
class QuerySpec:
    dataset: str
    select: tuple[str, ...]
    where: tuple[Cond, ...]
    group_by: tuple[str, ...]
    metrics: tuple[str, ...]
    time_bucket: tuple[str, str] | None
    order: tuple[tuple[str, bool], ...]  # (output name, descending)
    limit: int
    include_outdated: bool
    as_of: datetime | None


def _bad(name: str) -> KhError:
    return kh_error("bad_arg", errors.BAD_ARG, name=name)


def _field(ds: Dataset, name: Any) -> Column | None:
    if not isinstance(name, str):
        return None
    return _VIRTUAL.get(name) or ds.column(name)


def _filterable(ds: Dataset, name: Any) -> Column | None:
    col = _field(ds, name)
    return col if col is not None and col.filterable else None


def _names(value: Any, arg: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        raise _bad(arg)
    if len(set(value)) != len(value):
        raise _bad(arg)
    return tuple(value)


def _as_of(value: Any) -> datetime:
    """An ISO date means midnight UTC; anything else must be an ISO datetime with a time zone."""
    if isinstance(value, str) and len(value) == 10:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    return _timestamp(value)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("invalid timestamp")
    return when


def _value(col: Column, value: Any) -> Any:
    if value is None:
        raise ValueError("null")
    if col.type == "timestamp":
        return _timestamp(value)
    return coerce(col, value)


def _parse_where(ds: Dataset, text: Any) -> tuple[Cond, ...]:
    if text is None or text == "":
        return ()
    if not isinstance(text, str):
        raise _bad("where")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise _bad("where") from None
    if not isinstance(data, list) or len(data) > MAX_CONDS:
        raise _bad("where")
    out: list[Cond] = []
    for entry in data:
        if not isinstance(entry, dict) or not {"column", "op"} <= entry.keys() <= {"column", "op", "value"}:
            raise _bad("where")
        col = _filterable(ds, entry["column"])
        op = entry["op"]
        if col is None or not isinstance(op, str) or op not in OPS:
            raise _bad("where")
        if op in ORDERED_OPS and col.type not in ("number", "date", "timestamp"):
            raise _bad("where")
        if op in ("is_null", "not_null"):
            if entry.get("value") is not None:
                raise _bad("where")
            out.append(Cond(col.name, op, None))
            continue
        if "value" not in entry:
            raise _bad("where")
        try:
            if op == "in":
                raw = entry["value"]
                if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_IN:
                    raise ValueError("in")
                value: Any = [_value(col, v) for v in raw]
            else:
                value = _value(col, entry["value"])
        except ValueError:
            raise _bad("where") from None
        out.append(Cond(col.name, op, value))
    return tuple(out)


def _parse_metric(ds: Dataset, text: str) -> tuple[str, str | None, str]:
    """``(kind, column, output name)`` of one metric, or ``BAD_ARG metrics``."""
    if text == "count":
        return "count", None, "count"
    kind, sep, name = text.partition(":")
    col = _field(ds, name)
    if not sep or kind not in METRIC_KINDS or col is None:
        raise _bad("metrics")
    if kind in ("sum", "avg") and col.type != "number":
        raise _bad("metrics")
    if kind in ("min", "max") and col.type not in ("number", "date"):
        raise _bad("metrics")
    return kind, col.name, f"{kind}_{col.name}"


def _parse_bucket(ds: Dataset, text: Any) -> tuple[str, str] | None:
    if text is None or text == "":
        return None
    if not isinstance(text, str):
        raise _bad("time_bucket")
    name, sep, unit = text.partition(":")
    col = _filterable(ds, name)
    if not sep or col is None or col.type not in ("date", "timestamp") or unit not in UNITS:
        raise _bad("time_bucket")
    return col.name, unit


def parse_spec(
    ds: Dataset,
    *,
    select: Any,
    where: Any,
    group_by: Any,
    metrics: Any,
    time_bucket: Any,
    order: Any,
    limit: Any,
    include_outdated: Any,
    as_of: Any,
    max_rows: int,
) -> QuerySpec:
    """Check every argument against the catalog; ``KhError`` ``BAD_ARG`` names the argument."""
    select_t = _names(select, "select")
    for name in select_t:
        if _field(ds, name) is None:
            raise _bad("select")
    group_t = _names(group_by, "group_by")
    for name in group_t:
        if _filterable(ds, name) is None:
            raise _bad("group_by")
    metrics_t = _names(metrics, "metrics")
    for text in metrics_t:
        _parse_metric(ds, text)
    bucket = _parse_bucket(ds, time_bucket)
    cond_t = _parse_where(ds, where)
    grouped = bool(metrics_t or group_t or bucket)
    if grouped and not set(select_t) <= set(group_t):
        raise _bad("select")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_rows:
        raise _bad("limit")
    if not isinstance(include_outdated, bool):
        raise _bad("include_outdated")
    when: datetime | None = None
    if as_of not in (None, ""):
        try:
            when = _as_of(as_of)
        except ValueError:
            raise _bad("as_of") from None
        if not ds.history:
            raise _bad("as_of")
    spec = QuerySpec(
        dataset=ds.name,
        select=select_t,
        where=cond_t,
        group_by=group_t,
        metrics=metrics_t,
        time_bucket=bucket,
        order=(),
        limit=limit,
        include_outdated=include_outdated,
        as_of=when,
    )
    outputs = _outputs(spec, ds)
    if len(set(outputs)) != len(outputs):
        raise _bad("metrics" if metrics_t else "select")
    order_t = _names(order, "order")
    parsed: list[tuple[str, bool]] = []
    for item in order_t:
        desc = item.startswith("-")
        name = item[1:] if desc else item
        if name not in outputs and (grouped or _field(ds, name) is None):
            raise _bad("order")
        parsed.append((name, desc))
    if len({n for n, _ in parsed}) != len(parsed):
        raise _bad("order")
    if not parsed and bucket:
        parsed = [("bucket", False)]
    return replace(spec, order=tuple(parsed))


def _outputs(spec: QuerySpec, ds: Dataset) -> list[str]:
    if spec.metrics or spec.group_by or spec.time_bucket:
        names = list(spec.select or spec.group_by)
        if spec.time_bucket:
            names.append("bucket")
        names.extend(_parse_metric(ds, m)[2] for m in spec.metrics)
        return names
    if spec.select:
        return list(spec.select)
    return ["_key", *(c.name for c in ds.columns)]


def _bucket_expr(ds: Dataset, bucket: tuple[str, str]) -> sql.Composable:
    name, unit = bucket
    col = sql.Identifier(name)
    if _field(ds, name).type == "timestamp":  # type: ignore[union-attr]
        col = sql.SQL("({} AT TIME ZONE 'UTC')").format(col)
    return sql.SQL("date_trunc('{}', {})::date").format(sql.SQL(unit), col)


def _metric_expr(ds: Dataset, text: str) -> sql.Composable:
    kind, name, _ = _parse_metric(ds, text)
    if kind == "count":
        return sql.SQL("count(*)")
    col = sql.Identifier(name)  # type: ignore[arg-type]
    if kind == "count_distinct":
        return sql.SQL("count(DISTINCT {})").format(col)
    return sql.SQL("{}({})").format(sql.SQL(kind), col)


def _cond_sql(cond: Cond, params: list[Any]) -> sql.Composable:
    col = sql.Identifier(cond.column)
    if cond.op == "is_null":
        return sql.SQL("{} IS NULL").format(col)
    if cond.op == "not_null":
        return sql.SQL("{} IS NOT NULL").format(col)
    params.append(cond.value)
    if cond.op == "in":
        return sql.SQL("{} = ANY(%s)").format(col)
    return sql.SQL("{} {} %s").format(col, sql.SQL(_SQL_OPS[cond.op]))


def build_query(
    spec: QuerySpec, ds: Dataset, audiences: list[str]
) -> tuple[sql.Composed, list[Any], list[str]]:
    """The statement, its parameters and the output column names; ``as_of`` reads the history twin."""
    outputs = _outputs(spec, ds)
    grouped = bool(spec.metrics or spec.group_by or spec.time_bucket)
    items: list[sql.Composable] = []
    if grouped:
        for name in spec.select or spec.group_by:
            items.append(sql.Identifier(name))
        if spec.time_bucket:
            items.append(
                sql.SQL("{} AS {}").format(_bucket_expr(ds, spec.time_bucket), sql.Identifier("bucket"))
            )
        for text in spec.metrics:
            items.append(
                sql.SQL("{} AS {}").format(_metric_expr(ds, text), sql.Identifier(_parse_metric(ds, text)[2]))
            )
    else:
        items = [sql.Identifier(n) for n in outputs]
    params: list[Any] = [list(audiences)]
    where: list[sql.Composable] = [sql.SQL("_audience = ANY(%s)")]
    if not spec.include_outdated:
        where.append(sql.SQL('NOT "_outdated"'))
    if spec.as_of is not None:
        where.append(sql.SQL('"_valid_from" <= %s AND ("_valid_to" IS NULL OR "_valid_to" > %s)'))
        params.extend([spec.as_of, spec.as_of])
    where.extend(_cond_sql(c, params) for c in spec.where)
    parts: list[sql.Composable] = [
        sql.SQL("SELECT {} FROM {}.{} WHERE {}").format(
            sql.SQL(", ").join(items),
            sql.Identifier("kh_data"),
            sql.Identifier(ds.name + "__h" if spec.as_of is not None else ds.name),
            sql.SQL(" AND ").join(where),
        )
    ]
    if grouped:
        keys: list[sql.Composable] = [sql.Identifier(n) for n in spec.group_by]
        if spec.time_bucket:
            keys.append(_bucket_expr(ds, spec.time_bucket))
        if keys:
            parts.append(sql.SQL("GROUP BY {}").format(sql.SQL(", ").join(keys)))
    if spec.order:
        parts.append(
            sql.SQL("ORDER BY {}").format(
                sql.SQL(", ").join(
                    sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL("DESC" if d else "ASC"))
                    for n, d in spec.order
                )
            )
        )
    parts.append(sql.SQL("LIMIT {}").format(sql.SQL(str(int(spec.limit) + 1))))
    return sql.SQL(" ").join(parts), params, outputs
