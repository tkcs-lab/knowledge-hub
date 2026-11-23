"""Write rows of a dataset: parse, upsert, delete and finish a sync (plan 3.3, 3.5)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import sql

from knowledge_hub.datasets.model import PG_TYPES, SYNC_RE, Column, Dataset, to_json_value
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import kh_error

__all__ = ["MAX_ROWS", "RowError", "RowIn", "coerce", "delete", "finish", "parse_rows", "upsert"]

MAX_ROWS = 500
MAX_VALUE_CHARS = 10000
_KEYS = frozenset({"key", "source_updated_at", "fields"})


@dataclass(frozen=True)
class RowIn:
    key: str
    source_updated_at: datetime
    values: dict[str, Any]


class RowError(Exception):
    def __init__(self, index: int, problem: str) -> None:
        super().__init__(f"row {index}: {problem}")
        self.index = index
        self.problem = problem


def _text(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_VALUE_CHARS or "\x00" in value:
        raise ValueError("invalid text")
    return value


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("invalid number")
    try:
        number = Decimal(str(value).strip() if isinstance(value, str) else repr(value))
    except InvalidOperation:
        raise ValueError("invalid number") from None
    if not number.is_finite():
        raise ValueError("invalid number")
    return number


def _date(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("invalid date")
    text = value.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        raise ValueError("invalid date") from None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in ("true", "false", "1", "0"):
        return value in ("true", "1")
    raise ValueError("invalid bool")


def coerce(col: Column, value: Any) -> Any:
    """The Python value for ``value`` in column ``col``; ``ValueError`` carries the problem text."""
    if value is None:
        return None
    try:
        if col.type in ("text", "ref"):
            return _text(value)
        if col.type == "number":
            return _number(value)
        if col.type == "date":
            return _date(value)
        if col.type == "bool":
            return _bool(value)
        if col.type == "choice":
            text = _text(value)
            if text not in col.choices:
                raise ValueError(f"{col.name} is not one of the choices")
            return text
    except ValueError as exc:
        if str(exc).endswith("is not one of the choices"):
            raise
        raise ValueError(f"{col.name} is not a valid {col.type}") from None
    raise ValueError(f"{col.name} is not a valid {col.type}")


def _key(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or "\x00" in value:
        raise ValueError("key is invalid")
    return value


def _updated(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("source_updated_at is invalid")
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("source_updated_at is invalid") from None
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("source_updated_at is invalid")
    return when


def parse_rows(text: str, ds: Dataset) -> list[RowIn]:
    """Parse the ``rows`` argument strictly; ``KhError`` for its shape, ``RowError`` per row."""
    bad = kh_error("bad_arg", errors.BAD_ARG, name="rows")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise bad from None
    if not isinstance(data, list) or not data:
        raise bad
    if any(not isinstance(e, dict) or e.keys() != _KEYS for e in data):
        raise bad
    if len(data) > MAX_ROWS:
        raise kh_error("too_many_rows", errors.TOO_MANY_ROWS)
    out: list[RowIn] = []
    seen: set[str] = set()
    first: frozenset[str] | None = None
    for i, entry in enumerate(data):
        try:
            key = _key(entry["key"])
        except ValueError as exc:
            raise RowError(i, str(exc)) from None
        try:
            when = _updated(entry["source_updated_at"])
        except ValueError as exc:
            raise RowError(i, str(exc)) from None
        fields = entry["fields"]
        if not isinstance(fields, dict):
            raise RowError(i, "fields is not an object")
        values: dict[str, Any] = {}
        for name, raw in fields.items():
            col = ds.field(name)
            if col is None:
                raise RowError(i, f"unknown field {str(name)[:64]}")
            try:
                values[col.name] = coerce(col, raw)
            except ValueError as exc:
                raise RowError(i, str(exc)) from None
        if key in seen:
            raise RowError(i, "duplicate key")
        seen.add(key)
        names = frozenset(values)
        if first is None:
            first = names
        elif names != first:
            raise RowError(i, "fields differ from row 0")
        out.append(RowIn(key, when, values))
    return out


def _json_row(row: RowIn, names: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"_key": row.key, "_source_updated_at": row.source_updated_at.isoformat()}
    for name in names:
        raw = row.values[name]
        out[name] = str(raw) if isinstance(raw, Decimal) else to_json_value(raw)
    return out


def _bad_sync() -> Exception:
    return kh_error("bad_arg", errors.BAD_ARG, name="sync_id")


_NEWER = sql.SQL("EXCLUDED._source_updated_at >= t._source_updated_at")
_CLEARS = sql.SQL("({newer} OR t._outdated_reason = 'sync')").format(newer=_NEWER)


def _upsert_sql(ds: Dataset, names: list[str]) -> sql.Composed:
    cols = [ds.column(n) for n in names]
    ids = [sql.Identifier(n) for n in names]
    insert_cols = sql.SQL(", ").join(
        [sql.Identifier(c) for c in ("_key", "_audience", "_source_updated_at", "_sync_id")] + ids
    )
    select_cols = sql.SQL(", ").join(
        [sql.SQL("x._key"), sql.SQL("%(aud)s"), sql.SQL("x._source_updated_at"), sql.SQL("%(sync)s")]
        + [sql.SQL("x.") + i for i in ids]
    )
    recordset = sql.SQL(", ").join(
        [sql.SQL("_key text"), sql.SQL("_source_updated_at timestamptz")]
        + [i + sql.SQL(" ") + sql.SQL(PG_TYPES[c.type]) for i, c in zip(ids, cols, strict=True)]
    )
    sets = [
        sql.SQL("_seen_at = now()"),
        sql.SQL("_sync_id = CASE WHEN %(sync)s <> '' THEN %(sync)s ELSE t._sync_id END"),
    ]
    for i in ids:
        sets.append(
            sql.SQL("{c} = CASE WHEN {newer} THEN EXCLUDED.{c} ELSE t.{c} END").format(c=i, newer=_NEWER)
        )
    sets.append(sql.SQL("_source_updated_at = greatest(t._source_updated_at, EXCLUDED._source_updated_at)"))
    sets.append(
        sql.SQL("_outdated = CASE WHEN {c} THEN false ELSE t._outdated END").format(c=_CLEARS)
    )
    sets.append(
        sql.SQL("_outdated_reason = CASE WHEN {c} THEN '' ELSE t._outdated_reason END").format(c=_CLEARS)
    )
    sets.append(sql.SQL("_outdated_at = CASE WHEN {c} THEN NULL ELSE t._outdated_at END").format(c=_CLEARS))
    return sql.SQL(
        "INSERT INTO kh_data.{table} AS t ({insert_cols}) SELECT {select_cols}"
        " FROM jsonb_to_recordset(%(rows)s::jsonb) AS x({recordset})"
        " ON CONFLICT (_audience, _key) DO UPDATE SET {sets} RETURNING (xmax = 0) AS inserted"
    ).format(
        table=sql.Identifier(ds.name),
        insert_cols=insert_cols,
        select_cols=select_cols,
        recordset=recordset,
        sets=sql.SQL(", ").join(sets),
    )


async def _sync_row(conn: Any, ds: Dataset, audience: str, sync_id: str) -> tuple[Any, ...] | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT finished_at FROM kh.dataset_syncs WHERE dataset = %s AND audience = %s AND sync_id = %s",
            (ds.name, audience, sync_id),
        )
        return await cur.fetchone()


async def upsert(
    conn: Any, ds: Dataset, audience: str, rows: list[RowIn], sync_id: str
) -> dict[str, int]:
    names = sorted({n for r in rows for n in r.values})
    if sync_id:
        found = await _sync_row(conn, ds, audience, sync_id)
        if found is not None and found[0] is not None:
            raise _bad_sync()
    payload = json.dumps([_json_row(r, names) for r in rows])
    async with conn.cursor() as cur:
        await cur.execute(_upsert_sql(ds, names), {"aud": audience, "sync": sync_id, "rows": payload})
        flags = await cur.fetchall()
    inserted = sum(1 for f in flags if f[0])
    if sync_id:
        await conn.execute(
            "INSERT INTO kh.dataset_syncs (dataset, audience, sync_id, rows_seen) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (dataset, audience, sync_id)"
            " DO UPDATE SET rows_seen = kh.dataset_syncs.rows_seen + EXCLUDED.rows_seen",
            (ds.name, audience, sync_id, len(rows)),
        )
    return {"rows": len(rows), "inserted": inserted, "updated": len(rows) - inserted}


async def delete(conn: Any, ds: Dataset, audience: str, keys: list[str]) -> dict[str, int]:
    query = sql.SQL(
        "UPDATE kh_data.{table} SET _outdated = true, _outdated_reason = 'delete', _outdated_at = now()"
        " WHERE _audience = %s AND _key = ANY(%s) AND NOT _outdated"
    ).format(table=sql.Identifier(ds.name))
    async with conn.cursor() as cur:
        await cur.execute(query, (audience, keys))
        return {"outdated": cur.rowcount}


async def finish(
    conn: Any, ds: Dataset, audience: str, sync_id: str, force: bool
) -> dict[str, Any]:
    if not SYNC_RE.fullmatch(sync_id):
        raise _bad_sync()
    found = await _sync_row(conn, ds, audience, sync_id)
    if found is None or found[0] is not None:
        raise _bad_sync()
    table = sql.Identifier(ds.name)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT started_at FROM kh.dataset_syncs WHERE dataset = %s AND audience = %s AND sync_id = %s",
            (ds.name, audience, sync_id),
        )
        started = (await cur.fetchone())[0]
        await cur.execute(
            sql.SQL(
                "SELECT count(*) FILTER (WHERE NOT _outdated),"
                " count(*) FILTER (WHERE NOT _outdated AND _seen_at < %s)"
                " FROM kh_data.{t} WHERE _audience = %s"
            ).format(t=table),
            (started, audience),
        )
        live, stale = await cur.fetchone()
        if live > 0 and stale * 100 > live * ds.max_outdated_percent and not force:
            raise kh_error(
                "sync_shrink",
                errors.SYNC_SHRINK,
                n=stale,
                live=live,
                percent=ds.max_outdated_percent,
            )
        await cur.execute(
            sql.SQL(
                "UPDATE kh_data.{t} SET _outdated = true, _outdated_reason = 'sync', _outdated_at = now()"
                " WHERE _audience = %s AND NOT _outdated AND _seen_at < %s"
            ).format(t=table),
            (audience, started),
        )
        outdated = cur.rowcount
        await cur.execute(
            sql.SQL(
                "SELECT max(_source_updated_at) FROM kh_data.{t} WHERE _audience = %s AND _seen_at >= %s"
            ).format(t=table),
            (audience, started),
        )
        watermark = (await cur.fetchone())[0]
        await cur.execute(
            "UPDATE kh.dataset_syncs SET finished_at = now(), outdated = %s, watermark = %s"
            " WHERE dataset = %s AND audience = %s AND sync_id = %s",
            (outdated, watermark, ds.name, audience, sync_id),
        )
    return {
        "outdated": outdated,
        "live": live - outdated,
        "watermark": watermark.isoformat() if watermark is not None else "",
    }
