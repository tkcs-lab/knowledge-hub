"""Read the dataset catalog through a caller's connection, under its row-level security (plan 3.3)."""

from __future__ import annotations

from typing import Any

from knowledge_hub.datasets.model import Column, Dataset

_DATASET_FIELDS = "name, title, description, audiences, title_column, history, max_outdated_percent"
_COLUMN_FIELDS = (
    "dataset, name, type, label, ordinal, choices, ref_dataset, source_name,"
    " filterable, embed, sensitive, indexed"
)


def _column(c: Any) -> Column:
    return Column(
        name=c[1],
        type=c[2],
        label=c[3],
        ordinal=c[4],
        choices=tuple(c[5]),
        ref_dataset=c[6],
        source_name=c[7],
        filterable=c[8],
        embed=c[9],
        sensitive=c[10],
        indexed=c[11],
    )


def _dataset(row: Any, cols: list[Column]) -> Dataset:
    return Dataset(
        name=row[0],
        title=row[1],
        description=row[2],
        audiences=tuple(row[3]),
        title_column=row[4],
        history=row[5],
        max_outdated_percent=row[6],
        columns=tuple(cols),
    )


async def load_dataset(conn: Any, name: str) -> Dataset | None:
    """The dataset ``name`` when the connection's caller may see it, otherwise ``None``."""
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {_DATASET_FIELDS} FROM kh.datasets WHERE name = %s", (name,))
        row = await cur.fetchone()
        if row is None:
            return None
        await cur.execute(
            f"SELECT {_COLUMN_FIELDS} FROM kh.columns WHERE dataset = %s ORDER BY ordinal", (name,)
        )
        cols = [_column(c) for c in await cur.fetchall()]
    return _dataset(row, cols)


async def load_catalog(conn: Any) -> list[Dataset]:
    """Every dataset the connection's caller may see, by name."""
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {_DATASET_FIELDS} FROM kh.datasets ORDER BY name")
        rows = await cur.fetchall()
        await cur.execute(f"SELECT {_COLUMN_FIELDS} FROM kh.columns ORDER BY dataset, ordinal")
        by_ds: dict[str, list[Column]] = {}
        for c in await cur.fetchall():
            by_ds.setdefault(c[0], []).append(_column(c))
    return [_dataset(r, by_ds.get(r[0], [])) for r in rows]
