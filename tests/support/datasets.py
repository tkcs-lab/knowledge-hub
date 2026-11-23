"""Create datasets for tests through the real DDL (plan 3.2)."""

from __future__ import annotations

import psycopg

from knowledge_hub.datasets import ddl
from knowledge_hub.datasets.model import Column, Dataset, DatasetError
from tests.support.pg import FRESH_DB

DEFAULT_COLUMNS = (
    Column("name", "text", "Name", 0, embed=True, filterable=True),
    Column("stage", "choice", "Stage", 0, choices=("open", "won", "lost"), embed=True, filterable=True),
    Column("amount", "number", "amount", 0, filterable=True),
    Column("close_date", "date", "close_date", 0, filterable=True),
    Column("secret_note", "text", "secret_note", 0, sensitive=True),
    Column("account", "ref", "account", 0, ref_dataset="accounts"),
)


def migrator_dsn(cluster) -> str:
    return cluster.dsn("kh_migrator", FRESH_DB)


def make_dataset(
    fresh_db,
    name: str = "deals",
    audiences: tuple[str, ...] = ("all", "mgmt"),
    columns: tuple[Column, ...] = DEFAULT_COLUMNS,
    title_column: str = "name",
) -> Dataset:
    """Create ``name`` (and ``accounts`` first when a ref column names it) and return the catalog entry."""
    dsn = migrator_dsn(fresh_db)
    if any(c.ref_dataset == "accounts" for c in columns) and name != "accounts":
        try:
            ddl.get_dataset(dsn, "accounts")
        except DatasetError:
            ddl.create_dataset(dsn, "accounts", "Accounts", audiences)
            ddl.add_column(dsn, "accounts", Column("name", "text", "Name", 0, embed=True))
    ddl.create_dataset(dsn, name, name.capitalize(), audiences)
    for col in columns:
        ddl.add_column(dsn, name, col)
    if title_column:
        # set_dataset arrives in p3, so the title column is set directly
        with psycopg.connect(dsn) as conn:
            conn.execute("UPDATE kh.datasets SET title_column = %s WHERE name = %s", (title_column, name))
            ds = ddl._load(conn, name)
            ddl._rebuild_mirror_trigger(conn, ds)
    return ddl.get_dataset(dsn, name)
