"""DDL for datasets: data tables in ``kh_data`` and their catalog rows (plan 3.2).

Every function takes the migrator DSN first, uses one connection and one transaction, and raises
``DatasetError`` for a refusal. Identifiers go only through ``sql.Identifier``; values are parameters.
"""

from __future__ import annotations

from collections.abc import Sequence

import psycopg
from psycopg import sql

from knowledge_hub.datasets.model import (
    PG_TYPES,
    Column,
    Dataset,
    DatasetError,
    check_dataset_name,
    check_new_column,
    check_text,
    object_name,
)

_COLUMN_FIELDS = (
    "name, type, label, ordinal, choices, ref_dataset, source_name, filterable, embed, sensitive, indexed"
)


def _connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn)


def _begin(conn: psycopg.Connection) -> None:
    conn.execute("SET LOCAL lock_timeout = '5s'")


def _load(conn: psycopg.Connection, name: str) -> Dataset | None:
    row = conn.execute(
        "SELECT name, title, description, audiences, title_column, history, max_outdated_percent"
        " FROM kh.datasets WHERE name = %s",
        (name,),
    ).fetchone()
    if row is None:
        return None
    cols = conn.execute(
        f"SELECT {_COLUMN_FIELDS} FROM kh.columns WHERE dataset = %s ORDER BY ordinal", (name,)
    ).fetchall()
    return Dataset(
        name=row[0],
        title=row[1],
        description=row[2],
        audiences=tuple(row[3]),
        title_column=row[4],
        history=row[5],
        max_outdated_percent=row[6],
        columns=tuple(
            Column(
                name=c[0],
                type=c[1],
                label=c[2],
                ordinal=c[3],
                choices=tuple(c[4]),
                ref_dataset=c[5],
                source_name=c[6],
                filterable=c[7],
                embed=c[8],
                sensitive=c[9],
                indexed=c[10],
            )
            for c in cols
        ),
    )


def get_dataset(dsn: str, name: str) -> Dataset:
    with _connect(dsn) as conn:
        ds = _load(conn, name)
    if ds is None:
        raise DatasetError(f"unknown dataset {name}")
    return ds


def list_catalog(dsn: str) -> list[Dataset]:
    with _connect(dsn) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM kh.datasets ORDER BY name").fetchall()]
        return [ds for ds in (_load(conn, n) for n in names) if ds is not None]


def _check_audiences(conn: psycopg.Connection, audiences: Sequence[str]) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(audiences))
    if not result:
        raise DatasetError("a dataset needs at least one audience")
    known = {r[0] for r in conn.execute("SELECT name FROM kh.groups WHERE name = ANY(%s)", (list(result),))}
    for audience in result:
        if audience not in known:
            raise DatasetError(f"audience {audience} is not a group")
    return result


def create_dataset(
    dsn: str, name: str, title: str, audiences: Sequence[str], description: str = ""
) -> Dataset:
    check_dataset_name(name)
    check_text(title, "title")
    if "\x00" in description:
        raise DatasetError("description is not valid")
    with _connect(dsn) as conn:
        _begin(conn)
        auds = _check_audiences(conn, audiences)
        if conn.execute("SELECT 1 FROM kh.datasets WHERE name = %s", (name,)).fetchone():
            raise DatasetError(f"dataset {name} exists")
        try:
            _create_table(conn, name)
        except psycopg.errors.DuplicateTable:
            raise DatasetError(f"dataset {name} exists") from None
        conn.execute(
            "INSERT INTO kh.datasets (name, title, description, audiences) VALUES (%s, %s, %s, %s)",
            (name, title, description, list(auds)),
        )
        ds = _load(conn, name)
        assert ds is not None
        _rebuild_mirror_trigger(conn, ds)
    return ds


def _create_table(conn: psycopg.Connection, name: str) -> None:
    t = sql.Identifier("kh_data", name)
    uq = sql.Identifier(object_name("uq", name, "_key"))
    conn.execute(
        sql.SQL(
            """
CREATE TABLE {t} (
  _id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  _key text NOT NULL CHECK (length(_key) BETWEEN 1 AND 200),
  _audience text NOT NULL REFERENCES kh.groups (name),
  _source_updated_at timestamptz NOT NULL,
  _seen_at timestamptz NOT NULL DEFAULT now(),
  _sync_id text NOT NULL DEFAULT '',
  _outdated boolean NOT NULL DEFAULT false,
  _outdated_reason text NOT NULL DEFAULT '' CHECK (_outdated_reason IN ('', 'sync', 'delete')),
  _outdated_at timestamptz,
  _created_at timestamptz NOT NULL DEFAULT now(),
  _updated_at timestamptz NOT NULL DEFAULT now(),
  _updated_by text NOT NULL DEFAULT '',
  CONSTRAINT outdated_fields CHECK (
    _outdated = (_outdated_reason <> '') AND _outdated = (_outdated_at IS NOT NULL)),
  CONSTRAINT {uq} UNIQUE (_audience, _key)
)"""
        ).format(t=t, uq=uq)
    )
    for kind, part, col in (("ix", "_seen", "_seen_at"), ("ix", "_updated", "_source_updated_at")):
        conn.execute(
            sql.SQL("CREATE INDEX {i} ON {t} (_audience, {c})").format(
                i=sql.Identifier(object_name(kind, name, part)), t=t, c=sql.Identifier(col)
            )
        )
    conn.execute(sql.SQL("ALTER TABLE {t} ENABLE ROW LEVEL SECURITY").format(t=t))
    conn.execute(sql.SQL("ALTER TABLE {t} FORCE ROW LEVEL SECURITY").format(t=t))
    conn.execute(
        sql.SQL(
            "CREATE POLICY kh_migrator_all ON {t} FOR ALL TO kh_migrator USING (true) WITH CHECK (true)"
        ).format(t=t)
    )
    conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON {t} TO kh_app").format(t=t))
    conn.execute(sql.SQL("GRANT SELECT ON {t} TO kh_bi").format(t=t))
    d = sql.SQL(
        "_audience = ANY ((SELECT audiences FROM kh.datasets WHERE name = {n})::text[])"
    ).format(n=sql.Literal(name))
    p = sql.SQL("kh.can_read(_audience) AND {d}").format(d=d)
    for stmt in (
        "CREATE POLICY kh_app_select ON {t} FOR SELECT TO kh_app USING ({p})",
        "CREATE POLICY kh_app_insert ON {t} FOR INSERT TO kh_app WITH CHECK ({p})",
        "CREATE POLICY kh_app_update ON {t} FOR UPDATE TO kh_app USING ({p}) WITH CHECK ({p})",
        "CREATE POLICY kh_bi_select ON {t} FOR SELECT TO kh_bi"
        " USING (_audience = (SELECT kh.bi_group()) AND {d})",
    ):
        conn.execute(sql.SQL(stmt).format(t=t, p=p, d=d))
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER guard BEFORE INSERT OR UPDATE ON {t}"
            " FOR EACH ROW EXECUTE FUNCTION kh.dataset_row_guard()"
        ).format(t=t)
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER mirror_ins AFTER INSERT ON {t}"
            " FOR EACH ROW EXECUTE FUNCTION kh.dataset_row_sync()"
        ).format(t=t)
    )


def add_column(dsn: str, dataset: str, col: Column) -> Dataset:
    with _connect(dsn) as conn:
        _begin(conn)
        ds = _load(conn, dataset)
        if ds is None:
            raise DatasetError(f"unknown dataset {dataset}")
        check_new_column(ds, col)
        if col.ref_dataset and conn.execute(
            "SELECT 1 FROM kh.datasets WHERE name = %s", (col.ref_dataset,)
        ).fetchone() is None:
            raise DatasetError(f"unknown dataset {col.ref_dataset}")
        t = sql.Identifier("kh_data", dataset)
        c = sql.Identifier(col.name)
        conn.execute(
            sql.SQL("ALTER TABLE {t} ADD COLUMN {c} {ty}").format(t=t, c=c, ty=sql.SQL(PG_TYPES[col.type]))
        )
        if col.type == "choice":
            literals = sql.SQL(", ").join(sql.Literal(v) for v in col.choices)
            conn.execute(
                sql.SQL(
                    "ALTER TABLE {t} ADD CONSTRAINT {ck}"
                    " CHECK ({c} IS NULL OR {c} = ANY (ARRAY[{v}]::text[]))"
                ).format(
                    t=t, ck=sql.Identifier(object_name("ck", dataset, col.name)), c=c, v=literals
                )
            )
        if col.indexed:
            conn.execute(
                sql.SQL("CREATE INDEX {i} ON {t} ({c})").format(
                    i=sql.Identifier(object_name("ix", dataset, col.name)), t=t, c=c
                )
            )
        ordinal = conn.execute(
            "SELECT coalesce(max(ordinal), 0) + 1 FROM kh.columns WHERE dataset = %s", (dataset,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO kh.columns (dataset, name, ordinal, type, label, choices, ref_dataset,"
            " source_name, filterable, embed, sensitive, indexed)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                dataset,
                col.name,
                ordinal,
                col.type,
                col.label,
                list(col.choices),
                col.ref_dataset,
                col.source_name,
                col.filterable,
                col.embed,
                col.sensitive,
                col.indexed,
            ),
        )
        ds = _load(conn, dataset)
        assert ds is not None
        _rebuild_mirror_trigger(conn, ds)
        if col.embed:
            _queue_refresh(conn, dataset)
    return ds


def _rebuild_mirror_trigger(conn: psycopg.Connection, ds: Dataset) -> None:
    """Recreate ``mirror_upd`` so it fires only when something the mirror reads changes."""
    t = sql.Identifier("kh_data", ds.name)
    watched = [c.name for c in ds.columns if c.embed]
    if ds.title_column and ds.title_column not in watched:
        watched.append(ds.title_column)
    parts = [
        sql.SQL("OLD._outdated IS DISTINCT FROM NEW._outdated"),
        sql.SQL("OLD._source_updated_at IS DISTINCT FROM NEW._source_updated_at"),
        *(sql.SQL("OLD.{c} IS DISTINCT FROM NEW.{c}").format(c=sql.Identifier(n)) for n in watched),
    ]
    conn.execute(sql.SQL("DROP TRIGGER IF EXISTS mirror_upd ON {t}").format(t=t))
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER mirror_upd AFTER UPDATE ON {t} FOR EACH ROW WHEN ({w})"
            " EXECUTE FUNCTION kh.dataset_row_sync()"
        ).format(t=t, w=sql.SQL(" OR ").join(parts))
    )


def _queue_refresh(conn: psycopg.Connection, name: str) -> None:
    conn.execute(
        "INSERT INTO kh.index_jobs (kind, payload)"
        " SELECT 'dataset_refresh', jsonb_build_object('dataset', %s::text)"
        " WHERE NOT EXISTS (SELECT 1 FROM kh.index_jobs WHERE kind = 'dataset_refresh'"
        " AND state = 'queued' AND payload ->> 'dataset' = %s::text)",
        (name, name),
    )
