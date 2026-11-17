"""Table-level leak tests: what each named principal sees through ``caller_tx`` (plan 3.3)."""

from __future__ import annotations

import pytest
from psycopg import errors

from knowledge_hub.db.caller_tx import caller_tx
from tests.support.principals import (
    KH_CONFIG,
    alice_all,
    bob_mgmt,
    carol_hr,
    dave_none,
    multi,
    wf_token,
)
from tests.support.seed import add_record

CALLERS = {
    "alice_all": alice_all,
    "bob_mgmt": bob_mgmt,
    "carol_hr": carol_hr,
    "dave_none": dave_none,
    "multi": multi,
    "wf_token": wf_token,
}
# principal -> groups it reads
EXPECTED = {
    "alice_all": {"all"},
    "bob_mgmt": {"mgmt"},
    "carol_hr": {"hr"},
    "dave_none": set(),
    "multi": {"all", "mgmt"},
    "wf_token": {"all"},
}
TABLES = ("documents", "records", "chunks")


async def _col(pool, caller, sql, params=()):
    async with caller_tx(pool, caller) as conn:
        cur = await conn.execute(sql, params)
        return {row[0] for row in await cur.fetchall()}


def test_callers_come_from_the_shared_config():
    assert set(EXPECTED) == set(CALLERS)
    assert set().union(*EXPECTED.values()) <= set(KH_CONFIG.groups)
    for name, caller in CALLERS.items():
        assert caller.audiences == frozenset(EXPECTED[name]), name


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("who", list(EXPECTED))
async def test_a_caller_reads_only_held_audiences(app_pool, seeded_db, who, table):
    got = await _col(app_pool, CALLERS[who], f"SELECT DISTINCT audience FROM kh.{table}")
    assert got == EXPECTED[who]


@pytest.mark.parametrize("who", list(EXPECTED))
async def test_select_returns_exactly_the_held_rows(app_pool, seeded_db, who):
    held = EXPECTED[who]
    for table in ("documents", "records"):
        got = await _col(app_pool, CALLERS[who], f"SELECT id::text FROM kh.{table}")
        assert got == {seeded_db[table][g] for g in held}, table
    got = await _col(app_pool, CALLERS[who], "SELECT id::text FROM kh.chunks")
    want = {seeded_db["chunks"][g] for g in held} | {seeded_db["record_chunks"][g] for g in held}
    assert got == want


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("who", list(EXPECTED))
async def test_guessed_id_of_a_hidden_row_returns_nothing(app_pool, seeded_db, who, table):
    key = {"documents": "documents", "records": "records", "chunks": "chunks"}[table]
    for group in set(KH_CONFIG.groups) - EXPECTED[who]:
        got = await _col(app_pool, CALLERS[who], f"SELECT id::text FROM kh.{table} WHERE id = %s",
                         (seeded_db[key][group],))
        assert got == set(), f"{who} saw hidden {table} row of {group}"


@pytest.mark.parametrize("table", TABLES)
async def test_no_caller_settings_means_no_rows(app_pool, seeded_db, table):
    async with app_pool.connection() as conn:
        cur = await conn.execute(f"SELECT count(*) FROM kh.{table}")
        assert (await cur.fetchone())[0] == 0


@pytest.mark.parametrize("who", list(EXPECTED))
async def test_every_caller_reads_all_groups_and_labels(app_pool, seeded_db, who):
    assert await _col(app_pool, CALLERS[who], "SELECT name FROM kh.groups") == set(KH_CONFIG.groups)
    assert await _col(app_pool, CALLERS[who], "SELECT name FROM kh.labels") == {"ember", "other"}


@pytest.mark.parametrize("table", ["documents", "records", "chunks", "groups", "labels"])
async def test_app_cannot_update_or_delete(app_pool, seeded_db, table):
    col = {"groups": "repo", "labels": "description"}.get(table, "audience")
    for stmt in (f"UPDATE kh.{table} SET {col} = {col}", f"DELETE FROM kh.{table}"):
        with pytest.raises(errors.InsufficientPrivilege):
            async with caller_tx(app_pool, multi) as conn:
                await conn.execute(stmt)


@pytest.mark.parametrize("table", ["index_jobs", "repo_state", "index_alerts", "claim_jti"])
async def test_app_cannot_read_internal_tables(app_pool, seeded_db, table):
    with pytest.raises(errors.InsufficientPrivilege):
        async with caller_tx(app_pool, bob_mgmt) as conn:
            await conn.execute(f"SELECT * FROM kh.{table}")


async def test_caller_tx_settings_do_not_leak_to_the_next_transaction(app_pool, seeded_db):
    assert await _col(app_pool, alice_all, "SELECT DISTINCT audience FROM kh.records") == {"all"}
    async with app_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM kh.records")
        assert (await cur.fetchone())[0] == 0


async def test_caller_tx_sets_no_email(app_pool, seeded_db):
    async with caller_tx(app_pool, alice_all) as conn:
        cur = await conn.execute("SELECT current_setting('kh.email', true)")
        assert (await cur.fetchone())[0] in (None, "")


async def test_insert_through_caller_tx(app_pool, seeded_db):
    async with caller_tx(app_pool, alice_all) as conn:
        await conn.execute(
            "INSERT INTO kh.records (audience, source, source_ref, title, blob_key, excerpt,"
            " content_type, occurred_at, written_by, created_by_sub, created_by_type)"
            " VALUES ('all', 's', 'r', 't', 'k', 'e', 'text/plain', now(), 'kh-bot', %s, %s)",
            (alice_all.sub, alice_all.principal_type),
        )
    with pytest.raises(errors.InsufficientPrivilege):
        async with caller_tx(app_pool, alice_all) as conn:
            await conn.execute(
                "INSERT INTO kh.records (audience, source, source_ref, title, blob_key, excerpt,"
                " content_type, occurred_at, written_by, created_by_sub, created_by_type)"
                " VALUES ('hr', 's', 'r', 't', 'k', 'e', 'text/plain', now(), 'kh-bot', %s, %s)",
                (alice_all.sub, alice_all.principal_type),
            )


def test_add_record_helper_returns_text_ids(fresh_db, seeded_db):
    import psycopg

    with psycopg.connect(fresh_db.admin_dsn("kh_fresh")) as conn:
        rid = add_record(conn, audience="hr")
    assert isinstance(rid, str) and len(rid) == 36
