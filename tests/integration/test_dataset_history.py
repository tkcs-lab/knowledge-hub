"""Opt-in history per dataset: twin table, as_of queries and retention (plan 3.8)."""

from __future__ import annotations

import time

import psycopg
import pytest

from knowledge_hub.datasets import ddl
from knowledge_hub.datasets.model import Column
from knowledge_hub.mcp.errors import BAD_ARG
from tests.support.datasets import make_dataset, migrator_dsn
from tests.support.pg import FRESH_DB
from tests.support.principals import multi

pytestmark = pytest.mark.integration


@pytest.fixture
def world(seeded_db, fresh_db):
    make_dataset(fresh_db)
    return fresh_db


def mig(world):
    return psycopg.connect(migrator_dsn(world), autocommit=True)


def add_row(world, key, name="Deal", stage="open", audience="all"):
    with mig(world) as conn:
        conn.execute(
            "INSERT INTO kh_data.deals (_key, _audience, _source_updated_at, name, stage, amount)"
            " VALUES (%s, %s, now(), %s, %s, 10)",
            (key, audience, name, stage),
        )


def versions(world, key="a"):
    with mig(world) as conn:
        return conn.execute(
            "SELECT stage, _valid_to IS NULL FROM kh_data.deals__h WHERE _key = %s ORDER BY _hid", (key,)
        ).fetchall()


def enable(world):
    return ddl.set_dataset(migrator_dsn(world), "deals", history=True)


async def q(mcp_client, **args):
    return await mcp_client.call(multi, "query", {"dataset": "deals", "select": ["_key", "stage"], **args})


def rows(res):
    assert not res.is_error, res.text
    return sorted(tuple(r) for r in res.data["result"]["rows"])


def test_enabling_seeds_one_version_per_row(world):
    add_row(world, "a")
    add_row(world, "b", audience="mgmt")
    assert ddl.get_dataset(migrator_dsn(world), "deals").history is False
    ds = enable(world)
    assert ds.history is True
    with mig(world) as conn:
        got = conn.execute("SELECT _key, _valid_to FROM kh_data.deals__h ORDER BY _key").fetchall()
    assert got == [("a", None), ("b", None)]


def test_update_closes_and_opens_a_version(world):
    add_row(world, "a")
    enable(world)
    with mig(world) as conn:
        conn.execute("UPDATE kh_data.deals SET stage = 'won' WHERE _key = 'a'")
    assert versions(world) == [("open", False), ("won", True)]
    with mig(world) as conn:  # nothing listed changes: seen-at and sync id only
        conn.execute("UPDATE kh_data.deals SET _seen_at = now(), _sync_id = 's' WHERE _key = 'a'")
    assert versions(world) == [("open", False), ("won", True)]
    with mig(world) as conn:  # any user column opens a version
        conn.execute("UPDATE kh_data.deals SET secret_note = 'x' WHERE _key = 'a'")
    assert len(versions(world)) == 3


def test_insert_after_enabling_opens_a_version(world):
    enable(world)
    add_row(world, "a")
    assert versions(world) == [("open", True)]


async def test_as_of_reads_the_old_version(world, mcp_client):
    add_row(world, "a")
    enable(world)
    time.sleep(0.05)
    with mig(world) as conn:
        between = conn.execute("SELECT now()").fetchone()[0]
    time.sleep(0.05)
    with mig(world) as conn:
        conn.execute("UPDATE kh_data.deals SET stage = 'won' WHERE _key = 'a'")
    assert rows(await q(mcp_client, as_of=between.isoformat())) == [("a", "open")]
    assert rows(await q(mcp_client)) == [("a", "won")]
    assert rows(await q(mcp_client, as_of="2999-01-01")) == [("a", "won")]
    assert rows(await q(mcp_client, as_of="2000-01-01")) == []


async def test_as_of_outdated_version_and_refusals(world, mcp_client):
    add_row(world, "a")
    res = await q(mcp_client, as_of="2026-01-01")
    assert res.is_error and res.text.endswith(BAD_ARG.format(name="as_of"))  # no history yet
    enable(world)
    for bad in ("yesterday", "2026-01-01T10:00:00", "2026-13-01"):
        res = await q(mcp_client, as_of=bad)
        assert res.is_error and res.text.endswith(BAD_ARG.format(name="as_of")), bad
    with mig(world) as conn:
        conn.execute(
            "UPDATE kh_data.deals SET _outdated = true, _outdated_reason = 'delete',"
            " _outdated_at = now() WHERE _key = 'a'"
        )
    assert rows(await q(mcp_client, as_of="2999-01-01")) == []
    assert rows(await q(mcp_client, as_of="2999-01-01", include_outdated=True)) == [("a", "open")]


def test_app_role_cannot_write_the_twin(world):
    enable(world)
    with psycopg.connect(world.dsn("kh_app", FRESH_DB)) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO kh_data.deals__h (_row_id, _key, _audience, _source_updated_at, _outdated,"
                " _valid_from) VALUES (gen_random_uuid(), 'x', 'all', now(), false, now())"
            )


def test_column_changes_follow_the_twin(world):
    add_row(world, "a")
    enable(world)
    dsn = migrator_dsn(world)
    ddl.add_column(dsn, "deals", Column("region", "text", "Region", 0))
    with mig(world) as conn:
        conn.execute("UPDATE kh_data.deals SET region = 'emea' WHERE _key = 'a'")
        assert conn.execute("SELECT region FROM kh_data.deals__h WHERE _valid_to IS NULL").fetchall() == [
            ("emea",)
        ]
    ddl.rename_column(dsn, "deals", "region", "area")
    with mig(world) as conn:
        conn.execute("UPDATE kh_data.deals SET area = 'apac' WHERE _key = 'a'")
        assert conn.execute("SELECT area FROM kh_data.deals__h WHERE _valid_to IS NULL").fetchall() == [
            ("apac",)
        ]
    ddl.drop_column(dsn, "deals", "area")
    with mig(world) as conn:
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'kh_data' AND table_name = 'deals__h'"
        )}
        assert "area" not in cols and "region" not in cols
        conn.execute("UPDATE kh_data.deals SET stage = 'lost' WHERE _key = 'a'")
    assert versions(world)[-1] == ("lost", True)


def test_purge_keeps_open_versions(world):
    add_row(world, "a")
    enable(world)
    with mig(world) as conn:
        conn.execute("UPDATE kh_data.deals SET stage = 'won' WHERE _key = 'a'")
        conn.execute(
            "UPDATE kh_data.deals__h SET _valid_to = now() - interval '2 days' WHERE _valid_to IS NOT NULL"
        )
        assert conn.execute("SELECT kh.dataset_history_purge(3)").fetchone()[0] == 0
        assert conn.execute("SELECT kh.dataset_history_purge(1)").fetchone()[0] == 1
        assert conn.execute("SELECT stage, _valid_to FROM kh_data.deals__h").fetchall() == [("won", None)]
    with psycopg.connect(world.dsn("kh_indexer", FRESH_DB)) as conn:
        assert conn.execute("SELECT kh.dataset_history_purge(1)").fetchone()[0] == 0


def test_disabling_drops_the_twin(world):
    enable(world)
    ds = ddl.set_dataset(migrator_dsn(world), "deals", history=False)
    assert ds.history is False
    with mig(world) as conn:
        assert conn.execute("SELECT to_regclass('kh_data.deals__h')").fetchone()[0] is None
        assert conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'kh_data.deals'::regclass"
            " AND tgname IN ('history_ins', 'history_upd')"
        ).fetchone()[0] == 0
        conn.execute(
            "INSERT INTO kh_data.deals (_key, _audience, _source_updated_at, name)"
            " VALUES ('z', 'all', now(), 'Z')"
        )
