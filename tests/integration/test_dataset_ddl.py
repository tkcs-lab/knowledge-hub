"""Creating datasets and columns: table, RLS, policies, triggers, job queue (plan 3.2, p2)."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors

from knowledge_hub.datasets import ddl
from knowledge_hub.datasets.model import Column, DatasetError, object_name
from knowledge_hub.db.caller_tx import caller_tx
from tests.support.datasets import make_dataset, migrator_dsn
from tests.support.principals import wf_all, wf_hr

pytestmark = pytest.mark.integration

SYSTEM = (
    "_id _key _audience _source_updated_at _seen_at _sync_id _outdated _outdated_reason"
    " _outdated_at _created_at _updated_at _updated_by"
).split()


@pytest.fixture
def mig(seeded_db, fresh_db):
    with psycopg.connect(migrator_dsn(fresh_db)) as conn:
        yield conn
        conn.commit()


@pytest.fixture
def deals(seeded_db, fresh_db):
    return make_dataset(fresh_db)


def _insert(conn, key: str, audience: str, **extra):
    cols = ["_key", "_audience", "_source_updated_at", *extra]
    marks = ", ".join(["%s", "%s", "now()", *["%s"] * len(extra)])
    return conn.execute(
        f"INSERT INTO kh_data.deals ({', '.join(cols)}) VALUES ({marks}) RETURNING _id",
        [key, audience, *extra.values()],
    )


def test_table_rls_policies_triggers(mig, deals):
    cols = [r[0] for r in mig.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'kh_data' AND table_name = 'deals' ORDER BY ordinal_position"
    )]
    assert cols[:12] == SYSTEM
    assert cols[12:] == ["name", "stage", "amount", "close_date", "secret_note", "account"]
    assert mig.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'kh_data.deals'::regclass"
    ).fetchone() == (True, True)
    pols = {
        r[0]
        for r in mig.execute("SELECT polname FROM pg_policy WHERE polrelid = 'kh_data.deals'::regclass")
    }
    assert pols == {"kh_migrator_all", "kh_app_select", "kh_app_insert", "kh_app_update", "kh_bi_select"}
    trigs = {r[0] for r in mig.execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'kh_data.deals'::regclass AND NOT tgisinternal"
    )}
    assert trigs == {"guard", "mirror_ins", "mirror_upd"}
    # catalog
    ds = ddl.get_dataset(migrator_dsn_of(mig), "deals")
    assert [c.name for c in ds.columns] == ["name", "stage", "amount", "close_date", "secret_note", "account"]
    assert [c.ordinal for c in ds.columns] == [1, 2, 3, 4, 5, 6]
    assert ds.title_column == "name"
    assert {d.name for d in ddl.list_catalog(migrator_dsn_of(mig))} == {"accounts", "deals"}


def migrator_dsn_of(conn) -> str:
    return conn.info.dsn


async def test_audience_rules(app_pool, deals):
    async with caller_tx(app_pool, wf_all) as conn:
        await _insert(conn, "k1", "all")
        row = await (await conn.execute(
            "SELECT _updated_by, _seen_at IS NOT NULL FROM kh_data.deals")).fetchone()
        assert row == ("key:wf-all", True)
    async with caller_tx(app_pool, wf_hr) as conn:
        with pytest.raises(errors.InsufficientPrivilege):  # hr is held but not in the dataset
            await _insert(conn, "k2", "hr")
    async with caller_tx(app_pool, wf_all) as conn:
        with pytest.raises(errors.InsufficientPrivilege):  # mgmt is in the dataset but not held
            await _insert(conn, "k3", "mgmt")


async def test_no_delete_no_ddl_and_frozen_identity(app_pool, deals):
    async with caller_tx(app_pool, wf_all) as conn:
        await _insert(conn, "k1", "all")
    async with caller_tx(app_pool, wf_all) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            await conn.execute("DELETE FROM kh_data.deals")
    async with caller_tx(app_pool, wf_all) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            await conn.execute("ALTER TABLE kh_data.deals ADD COLUMN x text")
    async with caller_tx(app_pool, wf_all) as conn:
        with pytest.raises(psycopg.Error) as err:
            await conn.execute("UPDATE kh_data.deals SET _key = 'other'")
        assert err.value.sqlstate == "P0K0D"


def test_indexed_column_and_choice_check(mig, deals):
    dsn = migrator_dsn_of(mig)
    ddl.add_column(dsn, "deals", Column("region", "text", "Region", 0, indexed=True))
    name = object_name("ix", "deals", "region")
    assert mig.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname = 'kh_data' AND indexname = %s", (name,)
    ).fetchone()
    mig.execute("INSERT INTO kh_data.deals (_key, _audience, _source_updated_at, stage)"
                " VALUES ('ok', 'all', now(), 'won')")
    with pytest.raises(errors.CheckViolation):
        mig.execute("INSERT INTO kh_data.deals (_key, _audience, _source_updated_at, stage)"
                    " VALUES ('bad', 'all', now(), 'maybe')")


def test_refusals(fresh_db, seeded_db, deals):
    dsn = migrator_dsn(fresh_db)
    with pytest.raises(DatasetError, match="^unknown dataset nowhere$"):
        ddl.add_column(dsn, "deals", Column("link", "ref", "Link", 0, ref_dataset="nowhere"))
    with pytest.raises(DatasetError, match="^dataset deals exists$"):
        ddl.create_dataset(dsn, "deals", "Deals", ("all",))
    with pytest.raises(DatasetError, match="^audience ghost is not a group$"):
        ddl.create_dataset(dsn, "other", "Other", ("ghost",))
    with pytest.raises(DatasetError, match="^unknown dataset nope$"):
        ddl.get_dataset(dsn, "nope")
    with pytest.raises(DatasetError, match="^column stage exists$"):
        ddl.add_column(dsn, "deals", Column("stage", "text", "Stage", 0))


def _jobs(conn):
    return conn.execute(
        "SELECT payload ->> 'dataset' FROM kh.index_jobs WHERE kind = 'dataset_refresh'"
        " AND state = 'queued' ORDER BY 1"
    ).fetchall()


def test_embed_columns_queue_one_refresh_per_dataset(mig, seeded_db, fresh_db):
    dsn = migrator_dsn(fresh_db)
    ddl.create_dataset(dsn, "a", "A", ("all",))
    ddl.create_dataset(dsn, "b", "B", ("all",))
    ddl.add_column(dsn, "a", Column("n1", "text", "N1", 0, embed=True))
    ddl.add_column(dsn, "b", Column("n1", "text", "N1", 0, embed=True))
    ddl.add_column(dsn, "b", Column("n2", "text", "N2", 0, embed=True))
    ddl.add_column(dsn, "b", Column("plain", "text", "Plain", 0))
    assert _jobs(mig) == [("a",), ("b",)]


def test_mirror_trigger_watches_embed_columns(mig, deals):
    d = mig.execute("SELECT pg_get_triggerdef(oid) FROM pg_trigger"
                    " WHERE tgrelid = 'kh_data.deals'::regclass AND tgname = 'mirror_upd'").fetchone()[0]
    assert "OLD.name IS DISTINCT FROM new.name".lower() in d.lower()
    assert "stage" in d and "amount" not in d
    assert "_outdated" in d and "_source_updated_at" in d
