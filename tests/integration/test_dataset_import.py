"""kh dataset import: CSV files into a dataset through the row writer (plan 3.10)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import psycopg
import pytest

from knowledge_hub.datasets import cli
from knowledge_hub.datasets import rows as rowlib
from knowledge_hub.datasets.model import Column
from knowledge_hub.db.pool import make_app_pool, make_indexer_pool
from knowledge_hub.deps import Deps
from tests.support.bearer import TestBearerResolver
from tests.support.datasets import make_dataset
from tests.support.pg import FRESH_DB
from tests.support.principals import KH_CONFIG

pytestmark = pytest.mark.integration

ADMIN = "admin@example.test"
CONFIG = KH_CONFIG.model_copy(update={"admins": [ADMIN]})


@pytest.fixture(autouse=True)
def deals(seeded_db, fresh_db):
    return make_dataset(
        fresh_db,
        columns=(
            Column("name", "text", "Name", 0, embed=True, filterable=True),
            Column("amount", "number", "amount", 0, filterable=True, source_name="Betrag"),
        ),
    )


@pytest.fixture
def run(db_settings, monkeypatch, tmp_path, capsys):
    """Run ``kh dataset import`` in-process; returns ``(exit_code, output)``."""

    async def fake_build_deps(settings, config):
        deps = Deps(
            settings=settings,
            config=config,
            app_pool=make_app_pool(db_settings),
            indexer_pool=make_indexer_pool(db_settings),
            bearer=TestBearerResolver(config),
        )
        await deps.app_pool.open(wait=True)
        await deps.indexer_pool.open(wait=True)
        return deps

    monkeypatch.setattr(cli, "build_deps", fake_build_deps)
    monkeypatch.setattr(cli, "load_config", lambda path: CONFIG)
    monkeypatch.setattr(cli, "load_settings", lambda: db_settings)

    def go(*args: str, audience: str = "all") -> tuple[int, str]:
        config = str(tmp_path / "kh.yaml")
        code = cli.main(["import", "deals", *args, "--audience", audience, "--by", ADMIN, "--config", config])
        return code, capsys.readouterr().out.strip()

    return go


@pytest.fixture
def csv_file(tmp_path):
    def make(text: str, name: str = "deals.csv", encoding: str = "utf-8"):
        path = tmp_path / name
        path.write_bytes(text.encode(encoding))
        return str(path)

    return make


def q(fresh_db, sql, *params):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        return conn.execute(sql, params).fetchall()


def test_headers_by_name_and_source_name_with_null_and_bom(run, csv_file, fresh_db):
    text = (
        "key,name,Betrag,source_updated_at\n"
        "a,Alpha,12.5,2026-11-01T10:00:00Z\n"
        "b,,3,2026-11-02T10:00:00+01:00\n"
    )
    path = csv_file(text, encoding="utf-8-sig")
    code, out = run(path)
    assert code == 0, out
    assert json.loads(out) == {"inserted": 2, "outdated": 0, "rows": 2, "updated": 0}
    got = q(fresh_db, "SELECT _key, name, amount, _source_updated_at FROM kh_data.deals ORDER BY _key")
    assert [(k, n, float(a)) for k, n, a, _ in got] == [("a", "Alpha", 12.5), ("b", None, 3.0)]
    assert got[1][3].astimezone(UTC).hour == 9
    code, out = run(path)
    assert json.loads(out) == {"inserted": 0, "outdated": 0, "rows": 2, "updated": 2}


def test_custom_columns(run, csv_file, fresh_db):
    path = csv_file("id,name,changed\nx,X,2026-11-01T00:00:00Z\n")
    code, out = run(path, "--key-column", "id", "--updated-column", "changed")
    assert code == 0, out
    assert q(fresh_db, "SELECT _key, name FROM kh_data.deals") == [("x", "X")]


def test_unknown_header(run, csv_file):
    code, out = run(csv_file("key,name,bogus\na,A,1\n"))
    assert (code, out) == (1, "dataset: unknown header bogus")


def test_bad_number_names_the_csv_line(run, csv_file, fresh_db):
    when = "2026-11-01T00:00:00Z"
    path = csv_file(f"key,name,amount,source_updated_at\na,A,1,{when}\nb,B,abc,{when}\n")
    code, out = run(path)
    assert (code, out) == (1, "dataset: line 3: amount is not a valid number")
    assert q(fresh_db, "SELECT count(*) FROM kh_data.deals") == [(0,)]


def test_missing_updated_column_uses_file_mtime(run, csv_file, fresh_db):
    path = csv_file("key,name\na,A\n")
    stamp = datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))
    code, out = run(path)
    assert code == 0, out
    ((when,),) = q(fresh_db, "SELECT _source_updated_at FROM kh_data.deals")
    assert when == datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC)


def test_sync_finish_outdates_missing_rows(run, csv_file, fresh_db):
    head = "key,name,source_updated_at\n"
    code, out = run(csv_file(head + "a,A,2026-11-01T00:00:00Z\nb,B,2026-11-01T00:00:00Z\n"))
    assert code == 0, out
    path = csv_file(head + "a,A,2026-11-01T00:00:00Z\n", name="second.csv")
    code, out = run(path, "--sync-id", "s", "--finish", "--force")
    assert code == 0, out
    assert json.loads(out) == {"inserted": 0, "outdated": 1, "rows": 1, "updated": 1}
    got = q(fresh_db, "SELECT _key, _outdated, _outdated_reason FROM kh_data.deals ORDER BY _key")
    assert got == [("a", False, ""), ("b", True, "sync")]
    # a finished sync cannot take more rows
    code, out = run(path, "--sync-id", "s")
    assert code == 1 and out.startswith("dataset: ")


def test_shrink_guard_applies_without_force(run, csv_file):
    head = "key,name,source_updated_at\n"
    run(csv_file(head + "a,A,2026-11-01T00:00:00Z\nb,B,2026-11-01T00:00:00Z\n"))
    path = csv_file(head + "a,A,2026-11-01T00:00:00Z\n", name="two.csv")
    code, out = run(path, "--sync-id", "s", "--finish")
    assert code == 1 and "finish_sync would mark 1 of 2 rows outdated" in out


def test_batches_of_500(run, csv_file, fresh_db, monkeypatch):
    calls: list[int] = []
    real = rowlib.upsert

    async def counting(conn, ds, audience, parsed, sync_id):
        calls.append(len(parsed))
        return await real(conn, ds, audience, parsed, sync_id)

    monkeypatch.setattr(rowlib, "upsert", counting)
    lines = "".join(f"k{i},N{i},2026-11-01T00:00:00Z\n" for i in range(1200))
    code, out = run(csv_file("key,name,source_updated_at\n" + lines), "--sync-id", "big", "--finish")
    assert code == 0, out
    assert calls == [500, 500, 200]
    assert json.loads(out) == {"inserted": 1200, "outdated": 0, "rows": 1200, "updated": 0}
    assert q(fresh_db, "SELECT rows_seen FROM kh.dataset_syncs WHERE sync_id = 'big'") == [(1200,)]


def test_finish_needs_sync_id(run, csv_file):
    code, out = run(csv_file("key,name\na,A\n"), "--finish")
    assert (code, out) == (2, "dataset: --finish needs --sync-id")


def test_refusals(run, csv_file):
    path = csv_file("key,name\na,A\n")
    code, out = run(path, audience="hr")
    assert (code, out) == (1, "dataset: unknown dataset")
    code, out = run(csv_file("name\nA\n", name="nokey.csv"))
    assert (code, out) == (1, "dataset: the file has no key column")
