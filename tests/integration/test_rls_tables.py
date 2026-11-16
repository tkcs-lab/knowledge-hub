"""Table-level leak tests: what each named principal sees through ``caller_tx`` (plan 3.3)."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors

from knowledge_hub.db.caller_tx import caller_tx
from tests.support.pg import FRESH_DB
from tests.support.principals import PRINCIPALS
from tests.support.seed import CASES, VISIBILITY_TABLES

# Principal -> seed cases it satisfies. dave_none matches only the attendee-only row because a
# verified person with no audience still matches rows listing their email.
EXPECTED = {
    "alice_all": {"all"},
    "bob_mgmt": {"all", "mgmt", "all_mgmt", "mgmt_or_finance"},
    "carol_hr": {"all", "hr"},
    "dave_none": {"attendee"},
    "eve_nobody": set(),
    "wf_token": {"all"},
    "mgmt_only": {"mgmt", "mgmt_or_finance"},
    "finance_only": {"mgmt_or_finance"},
    "mgmt_finance": {"mgmt", "mgmt_or_finance", "mgmt_and_finance"},
}

# child table -> (parent visibility table, child column naming the parent)
CHILDREN = {
    "document_versions": ("documents", "document_id"),
    "derived_sources": ("derived_records", "derived_id"),
    "candidate_sources": ("candidates", "candidate_id"),
    "conflict_items": ("conflicts", "conflict_id"),
}

PRINCIPAL_TABLE = [(p, t) for p in EXPECTED for t in VISIBILITY_TABLES]


async def _ids(pool, caller, sql, params=()):
    async with caller_tx(pool, caller) as conn:
        cur = await conn.execute(sql, params)
        return {row[0] for row in await cur.fetchall()}


def test_expected_covers_every_principal():
    assert set(EXPECTED) == set(PRINCIPALS)
    assert set().union(*EXPECTED.values()) <= set(CASES)


@pytest.mark.parametrize(("who", "table"), PRINCIPAL_TABLE)
async def test_select_all_returns_exactly_visible_rows(app_pool, seeded_db, who, table):
    got = await _ids(app_pool, PRINCIPALS[who], f"SELECT id::text FROM kh.{table}")
    assert got == {seeded_db[table][c] for c in EXPECTED[who]}


@pytest.mark.parametrize(("who", "table"), PRINCIPAL_TABLE)
async def test_guessed_id_of_hidden_row_returns_nothing(app_pool, seeded_db, who, table):
    for case in set(CASES) - EXPECTED[who]:
        got = await _ids(
            app_pool, PRINCIPALS[who], f"SELECT id::text FROM kh.{table} WHERE id::text = %s",
            (seeded_db[table][case],),
        )
        assert got == set(), f"{who} saw hidden {table} row {case}"


@pytest.mark.parametrize("table", VISIBILITY_TABLES)
async def test_no_caller_settings_means_no_rows(app_pool, seeded_db, table):
    async with app_pool.connection() as conn:
        cur = await conn.execute(f"SELECT count(*) FROM kh.{table}")
        assert (await cur.fetchone())[0] == 0


@pytest.fixture
def all_tables(fresh_db, seeded_db):
    """``{table: first non-generated column}`` for every table in schema ``kh``."""
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        return dict(
            conn.execute(
                "SELECT c.relname, (SELECT a.attname FROM pg_attribute a WHERE a.attrelid = c.oid"
                " AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = '' ORDER BY a.attnum LIMIT 1)"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'kh' AND c.relkind = 'r' ORDER BY 1"
            ).fetchall()
        )


async def test_app_cannot_update_or_delete_any_table(app_pool, all_tables):
    assert {"documents", "index_jobs", "claim_jti", "schema_migrations"} <= set(all_tables)
    for table, col in all_tables.items():
        for stmt in (f'UPDATE kh.{table} SET "{col}" = DEFAULT', f"DELETE FROM kh.{table}"):
            with pytest.raises(errors.InsufficientPrivilege):
                async with caller_tx(app_pool, PRINCIPALS["bob_mgmt"]) as conn:
                    await conn.execute(stmt)


async def test_app_cannot_read_index_jobs(app_pool, seeded_db):
    with pytest.raises(errors.InsufficientPrivilege):
        async with caller_tx(app_pool, PRINCIPALS["bob_mgmt"]) as conn:
            await conn.execute("SELECT * FROM kh.index_jobs")


@pytest.mark.parametrize(("who", "child"), [(p, c) for p in EXPECTED for c in CHILDREN])
async def test_child_tables_follow_their_parent(app_pool, seeded_db, who, child):
    parent, col = CHILDREN[child]
    got = await _ids(app_pool, PRINCIPALS[who], f"SELECT {col}::text FROM kh.{child}")
    assert got == {seeded_db[parent][c] for c in EXPECTED[who]}
