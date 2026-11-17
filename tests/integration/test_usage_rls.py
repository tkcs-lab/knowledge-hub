"""usage_events (migration 0002): the insert check, the subset read rule, grants and shape checks."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import errors

from knowledge_hub.db.caller_tx import caller_tx
from tests.support.principals import alice_all, bob_mgmt, dave_none, multi, wf_token
from tests.support.seed import add_usage

DB = "kh_fresh"
INSERT = (
    "INSERT INTO kh.usage_events (id, kind, who, audiences, query, min_tier)"
    " VALUES (%s, 'search', %s, {aud}, %s, 'T3')"
)
OWN = INSERT.format(aud="kh.caller_audiences()")


@pytest.fixture
def world(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all'), ('mgmt', 'kb-mgmt')")
    return fresh_db


async def test_insert_for_own_groups_normalises_the_query(app_pool, world):
    async with caller_tx(app_pool, alice_all) as conn:
        await conn.execute(OWN, (str(uuid.uuid4()), "a" * 32, "  Hello   WORLD "))
        cur = await conn.execute("SELECT query_norm FROM kh.usage_events")
        assert await cur.fetchall() == [("hello world",)]


async def test_insert_for_other_groups_is_refused(app_pool, world):
    with pytest.raises(errors.InsufficientPrivilege):
        async with caller_tx(app_pool, alice_all) as conn:
            await conn.execute(INSERT.format(aud="ARRAY['mgmt']"), (str(uuid.uuid4()), "a" * 32, "q"))


@pytest.mark.parametrize("caller", [wf_token, dave_none], ids=["n8n", "no-groups"])
async def test_insert_by_workflow_or_groupless_caller_is_refused(app_pool, world, caller):
    with pytest.raises(errors.InsufficientPrivilege):
        async with caller_tx(app_pool, caller) as conn:
            await conn.execute(OWN, (str(uuid.uuid4()), "a" * 32, "q"))


async def _read(pool, caller) -> set[str]:
    async with caller_tx(pool, caller) as conn:
        cur = await conn.execute("SELECT id::text FROM kh.usage_events")
        return {r[0] for r in await cur.fetchall()}


async def test_subset_rule(app_pool, world):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        only_all = add_usage(conn, audiences=["all"])
        both = add_usage(conn, audiences=["all", "mgmt"])
    assert await _read(app_pool, alice_all) == {only_all}
    assert await _read(app_pool, multi) == {only_all, both}
    assert await _read(app_pool, bob_mgmt) == set()
    assert await _read(app_pool, dave_none) == set()
    assert await _read(app_pool, wf_token) == {only_all}


@pytest.mark.parametrize("stmt", ["UPDATE kh.usage_events SET kind = kind", "DELETE FROM kh.usage_events"])
async def test_app_cannot_update_or_delete(app_pool, world, stmt):
    with pytest.raises(errors.InsufficientPrivilege):
        async with caller_tx(app_pool, multi) as conn:
            await conn.execute(stmt)


@pytest.mark.parametrize(
    "fields",
    [
        dict(who="xyz"),
        dict(who="A" * 32),
        dict(query=None),
        dict(kind="open", query=None, min_tier=None, owner_keys=["a", "b"]),
        dict(query="x" * 501),
    ],
    ids=["who-not-hex", "who-uppercase", "search-without-query", "open-with-two-keys", "query-501"],
)
def test_check_constraints(world, fields):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        with pytest.raises(errors.CheckViolation):
            add_usage(conn, **fields)


def test_valid_open_event_is_accepted(world):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        add_usage(conn, kind="open", query=None, min_tier=None, owner_keys=["rec:1"])
        add_usage(conn, query="x" * 500)
