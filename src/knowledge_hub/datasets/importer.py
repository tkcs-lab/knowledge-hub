"""``kh dataset import``: load a CSV file into a dataset through the row writer (plan 3.10)."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from knowledge_hub.auth.claims import Caller
from knowledge_hub.datasets import rows
from knowledge_hub.datasets.catalog import load_dataset
from knowledge_hub.datasets.model import Dataset
from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.deps import Deps
from knowledge_hub.mcp import errors

__all__ = ["ImportRefused", "import_csv"]


class ImportRefused(Exception):
    """The import cannot run; ``code`` is the exit status of ``kh dataset import``."""

    def __init__(self, text: str, code: int = 1) -> None:
        super().__init__(text)
        self.code = code


def _records(reader: csv.DictReader[str]) -> Iterator[tuple[int, dict[str, str | None]]]:
    """Yield ``(line, record)``; ``line`` is the 1-based line where the record starts."""
    for record in reader:
        if None in record:
            raise ImportRefused(f"line {reader.line_num}: more cells than headers")
        spanned = sum(cell.count("\n") for cell in record.values() if isinstance(cell, str))
        yield reader.line_num - spanned, record


def _entry(
    record: dict[str, str | None], key_column: str, updated_column: str, mtime: str
) -> dict[str, Any]:
    special = (key_column, updated_column)
    fields = {h: (v or None) for h, v in record.items() if h not in special}
    updated = record.get(updated_column) if updated_column in record else mtime
    return {"key": record[key_column], "source_updated_at": updated, "fields": fields}


async def _send(
    deps: Deps,
    caller: Caller,
    ds: Dataset,
    audience: str,
    batch: list[tuple[int, dict[str, Any]]],
    sync_id: str,
) -> dict[str, int]:
    try:
        parsed = rows.parse_rows(json.dumps([e for _, e in batch]), ds)
        async with caller_tx(deps.app_pool, caller) as conn:
            return await rows.upsert(conn, ds, audience, parsed, sync_id)
    except rows.RowError as exc:
        raise ImportRefused(f"line {batch[exc.index][0]}: {exc.problem}") from None


async def import_csv(
    deps: Deps,
    path: Path,
    dataset: str,
    audience: str,
    email: str,
    key_column: str,
    updated_column: str,
    sync_id: str,
    finish: bool,
    force: bool,
) -> dict[str, int]:
    """Import ``path`` into ``dataset`` for ``audience`` as administrator ``email``."""
    if finish and not sync_id:
        raise ImportRefused("--finish needs --sync-id", 2)
    if not deps.config.is_active(audience):
        raise ImportRefused(f"audience {audience} is not an active group")
    caller = Caller(
        sub=f"admin:{email}",
        principal_type="admin",
        email=email,
        audiences=frozenset({audience}),
        token_id=None,
        tools=None,
    )
    async with caller_tx(deps.app_pool, caller) as conn:
        ds = await load_dataset(conn, dataset)
    if ds is None or audience not in ds.audiences:
        raise ImportRefused(errors.UNKNOWN_DATASET)
    try:
        handle = path.open(encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ImportRefused(f"cannot read {path}: {exc.strerror or type(exc).__name__}") from None
    with handle:
        try:
            return await _import(
                deps, caller, ds, handle, path, audience, key_column, updated_column, sync_id, finish, force
            )
        except (csv.Error, UnicodeDecodeError) as exc:
            raise ImportRefused(f"{path} is not a valid CSV file: {exc}") from None


async def _import(
    deps: Deps,
    caller: Caller,
    ds: Dataset,
    handle: Any,
    path: Path,
    audience: str,
    key_column: str,
    updated_column: str,
    sync_id: str,
    finish: bool,
    force: bool,
) -> dict[str, int]:
    reader = csv.DictReader(handle)
    headers = list(reader.fieldnames or [])
    if key_column not in headers:
        raise ImportRefused(f"the file has no {key_column} column")
    for header in headers:
        if header not in (key_column, updated_column) and ds.field(header) is None:
            raise ImportRefused(f"unknown header {header[:64]}")
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    total = {"rows": 0, "inserted": 0, "updated": 0, "outdated": 0}
    batch: list[tuple[int, dict[str, Any]]] = []

    async def flush() -> None:
        done = await _send(deps, caller, ds, audience, batch, sync_id)
        for name in ("rows", "inserted", "updated"):
            total[name] += done[name]
        batch.clear()

    for line, record in _records(reader):
        batch.append((line, _entry(record, key_column, updated_column, mtime)))
        if len(batch) == rows.MAX_ROWS:
            await flush()
    if batch:
        await flush()
    if finish:
        if total["rows"] == 0:
            raise ImportRefused("the file has no rows")
        async with caller_tx(deps.app_pool, caller) as conn:
            done = await rows.finish(conn, ds, audience, sync_id, force)
        total["outdated"] = done["outdated"]
    return total

