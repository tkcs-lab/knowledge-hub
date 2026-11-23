"""Migration 0007 part one: catalog tables, row-record marks, definer refusals, the kh_bi role (plan 3.1)."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import errors

from knowledge_hub.db.caller_tx import caller_tx
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, multi, wf_all
from tests.support.seed import add_record

pytestmark = pytest.mark.integration

NEW_TABLES = ("datasets", "columns", "saved_queries", "dataset_syncs", "bi_roles")
BI_LOGIN = "kh_bi_aaaaaaaaaa"


@pytest.fixture
def mig(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_migrator", FRESH_DB)) as conn:
        yield conn
        conn.commit()


def _dataset(conn, name: str, audiences: list[str]) -> None:
    conn.execute(
        "INSERT INTO kh.datasets (name, title, audiences) VALUES (%s, %s, %s)", (name, name, audiences)
    )


def _row_record(conn, audience: str = "all") -> str:
    ref = uuid.uuid4().hex
    return add_record(
        conn, audience=audience, source="dataset:x", source_ref=ref, dataset_row=f"x:{ref}"
    )


def test_every_new_table_forces_rls(mig):
    rows = mig.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
        " WHERE relnamespace = 'kh'::regnamespace AND relname = ANY(%s)", (list(NEW_TABLES),)
    ).fetchall()
    assert {r[0] for r in rows} == set(NEW_TABLES)
    assert all(r[1] and r[2] for r in rows)


async def test_datasets_are_visible_only_to_overlapping_audiences(mig, app_pool):
    _dataset(mig, "d_all", ["all"])
    _dataset(mig, "d_mgmt", ["mgmt"])
    mig.commit()
    async with caller_tx(app_pool, alice_all) as conn:
        cur = await conn.execute("SELECT name FROM kh.datasets ORDER BY name")
        assert [r[0] for r in await cur.fetchall()] == ["d_all"]
    async with caller_tx(app_pool, bob_mgmt) as conn:
        cur = await conn.execute("SELECT name FROM kh.datasets ORDER BY name")
        assert [r[0] for r in await cur.fetchall()] == ["d_mgmt"]


async def test_columns_follow_their_dataset(mig, app_pool):
    _dataset(mig, "d_all", ["all"])
    _dataset(mig, "d_mgmt", ["mgmt"])
    for ds in ("d_all", "d_mgmt"):
        mig.execute(
            "INSERT INTO kh.columns (dataset, name, ordinal, type, label)"
            " VALUES (%s, 'c', 1, 'text', 'C')", (ds,)
        )
    mig.commit()
    async with caller_tx(app_pool, alice_all) as conn:
        cur = await conn.execute("SELECT dataset FROM kh.columns")
        assert [r[0] for r in await cur.fetchall()] == ["d_all"]
    async with caller_tx(app_pool, bob_mgmt) as conn:
        cur = await conn.execute("SELECT dataset FROM kh.columns ORDER BY dataset")
        assert [r[0] for r in await cur.fetchall()] == ["d_mgmt"]


def test_dataset_row_ties_to_the_source_prefix_and_is_unique(mig):
    with pytest.raises(errors.CheckViolation):
        with mig.transaction():
            add_record(mig, source="dataset:x", dataset_row="")
    rec = _row_record(mig)
    ref = mig.execute("SELECT dataset_row FROM kh.records WHERE id = %s", (rec,)).fetchone()[0]
    with pytest.raises(errors.UniqueViolation):
        with mig.transaction():
            add_record(mig, source="dataset:x", source_ref="other", dataset_row=ref)


async def test_app_cannot_insert_a_row_record(mig, app_pool):
    with pytest.raises(errors.InsufficientPrivilege):
        async with caller_tx(app_pool, wf_all) as conn:
            await conn.execute(
                "INSERT INTO kh.records (audience, source, source_ref, title, blob_key, excerpt,"
                " content_type, occurred_at, written_by, created_by_sub, created_by_type, dataset_row)"
                " VALUES ('all', 'dataset:x', 'r', 't', 'b', 'e', 'text/plain', now(), 'w', %s, %s, 'x:r')",
                (wf_all.sub, wf_all.principal_type),
            )


async def test_mark_record_outdated_refuses_a_row_record(mig, app_pool):
    rec = _row_record(mig, "all")
    other = _row_record(mig, "hr")
    mig.commit()
    async with caller_tx(app_pool, alice_all) as conn:
        with pytest.raises(psycopg.Error) as exc:
            async with conn.transaction():
                await conn.execute("SELECT kh.mark_record_outdated(%s, true, '', '')", (rec,))
        assert exc.value.sqlstate == "P0K0B"
        cur = await conn.execute("SELECT kh.mark_record_outdated(%s, true, '', '')", (other,))
        assert (await cur.fetchone())[0] is None


async def test_move_record_refuses_a_row_record(mig, app_pool):
    rec = _row_record(mig, "all")
    mig.commit()
    async with caller_tx(app_pool, multi) as conn:
        with pytest.raises(psycopg.Error) as exc:
            async with conn.transaction():
                await conn.execute("SELECT kh.move_record(%s, 'all', 'mgmt')", (rec,))
        assert exc.value.sqlstate == "P0K0C"


def test_app_cannot_create_in_the_data_schema(mig):
    assert mig.execute("SELECT has_schema_privilege('kh_app', 'kh_data', 'CREATE')").fetchone() == (False,)


async def test_bi_group_comes_from_the_session_user(mig, fresh_db, app_pool):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as admin:
        admin.execute(f"CREATE ROLE {BI_LOGIN} LOGIN NOSUPERUSER NOBYPASSRLS IN ROLE kh_bi")
        try:
            mig.execute(
                "INSERT INTO kh.bi_roles (role_name, audience, created_by) VALUES (%s, 'all', 'test')",
                (BI_LOGIN,),
            )
            mig.commit()
            with psycopg.connect(fresh_db.dsn(BI_LOGIN, FRESH_DB)) as bi:
                assert bi.execute("SELECT kh.bi_group()").fetchone() == ("all",)
            async with caller_tx(app_pool, alice_all) as conn:
                with pytest.raises(errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute("SELECT kh.bi_group()")
        finally:
            mig.execute("DELETE FROM kh.bi_roles WHERE role_name = %s", (BI_LOGIN,))
            mig.commit()
            admin.execute(f"DROP ROLE {BI_LOGIN}")


def test_dataset_refresh_jobs_and_the_queued_index(mig):
    mig.execute("INSERT INTO kh.index_jobs (kind, payload) VALUES ('dataset_refresh', '{\"id\": \"x\"}')")
    assert mig.execute("SELECT to_regclass('kh.index_jobs_queued_idx') IS NOT NULL").fetchone() == (True,)
