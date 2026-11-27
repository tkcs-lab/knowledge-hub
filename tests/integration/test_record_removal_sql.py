"""Migration 0008: the removal reason, delete_records, restore_records, the purge functions (plan 83, 3.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors

from knowledge_hub.db.caller_tx import caller_tx
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, multi, wf_all
from tests.support.seed import add_chunked_record, add_record

pytestmark = pytest.mark.integration

DELETE = "SELECT kh.delete_records(%s, %s, %s::text[], %s, %s)"
RESTORE = "SELECT kh.restore_records(%s, %s, %s::text[], %s, %s::timestamptz)"
CLAIM = "SELECT rec_id, rec_blob_key FROM kh.records_purge_claim(%s, %s, %s::uuid[])"
PURGE_DELETE = "SELECT kh.records_purge_delete(%s::uuid[])"


@pytest.fixture
def db(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        yield conn
        conn.commit()


def _long_ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _removed(conn, *, reason: str = "delete", days: float = 0, **fields) -> str:
    """A record hidden ``days`` days ago with the given removal reason."""
    return add_record(
        conn, outdated=True, outdated_at=datetime.now(UTC) - timedelta(days=days), outdated_by="x",
        outdated_note="removed from source", outdated_reason=reason, **fields,
    )


def _state(conn, rec: str) -> tuple:
    conn.commit()
    return conn.execute(
        "SELECT outdated, outdated_reason, outdated_at, outdated_note FROM kh.records WHERE id = %s", (rec,)
    ).fetchone()


def _live(conn, source: str, refs: list[str]) -> dict[str, str]:
    return {ref: add_record(conn, source=source, source_ref=ref) for ref in refs}


def _hidden_refs(conn, source: str) -> set[str]:
    conn.commit()
    rows = conn.execute("SELECT source_ref FROM kh.records WHERE source = %s AND outdated", (source,))
    return {r[0] for r in rows}


async def _one(pool, caller, sql: str, *params):
    async with caller_tx(pool, caller) as conn:
        cur = await conn.execute(sql, params)
        return (await cur.fetchone())[0]


async def _fails(pool, caller, sql: str, *params) -> psycopg.Error:
    with pytest.raises(psycopg.Error) as exc:
        async with caller_tx(pool, caller) as conn:
            async with conn.transaction():
                await conn.execute(sql, params)
    return exc.value


def test_reason_column_and_constraints(db):
    rec = add_record(db)
    assert db.execute("SELECT outdated_reason FROM kh.records WHERE id = %s", (rec,)).fetchone() == ("",)
    ref = uuid.uuid4().hex
    cases = {
        "reason without outdated": dict(outdated_reason="delete"),
        "unknown reason": dict(outdated=True, outdated_at=_long_ago(0), outdated_reason="other"),
        "reason on a dataset row": dict(
            source="dataset:x", source_ref=ref, dataset_row=f"x:{ref}", outdated=True,
            outdated_at=_long_ago(0), outdated_reason="delete",
        ),
    }
    for name, fields in cases.items():
        with pytest.raises(errors.CheckViolation), db.transaction():
            add_record(db, **fields)
        assert name
    for reason in ("", "delete", "purge"):
        _removed(db, reason=reason)


async def test_delete_records_exact_refs(db, app_pool):
    refs = _live(db, "ex", ["a", "b", "c"])
    other = add_record(db, source="ex2", source_ref="a")
    db.commit()
    got = await _one(app_pool, wf_all, DELETE, "all", "ex", ["a", "c", "missing"], False, False)
    assert got == 2
    assert _hidden_refs(db, "ex") == {"a", "c"}
    assert _state(db, refs["a"])[:2] == (True, "delete")
    assert _state(db, refs["a"])[3] == "removed from source"
    assert _state(db, refs["b"])[:2] == (False, "")
    assert _state(db, other)[:2] == (False, "")
    by = db.execute("SELECT outdated_by FROM kh.records WHERE id = %s", (refs["a"],)).fetchone()
    assert by == (wf_all.sub,)


async def test_delete_records_prefix_is_literal(db, app_pool):
    refs = _live(db, "lit", ["a_b@1#p1of1", "axb@1#p1of1", "a%b@1#p1of1", "aXXXb@1#p1of1"])
    db.commit()
    assert await _one(app_pool, wf_all, DELETE, "all", "lit", ["a_b@1"], True, False) == 1
    assert _hidden_refs(db, "lit") == {"a_b@1#p1of1"}
    assert await _one(app_pool, wf_all, DELETE, "all", "lit", ["a%b@1"], True, False) == 1
    assert _hidden_refs(db, "lit") == {"a_b@1#p1of1", "a%b@1#p1of1"}
    assert _state(db, refs["axb@1#p1of1"])[0] is False
    assert _state(db, refs["aXXXb@1#p1of1"])[0] is False


async def test_delete_records_keeps_the_clock_of_removed_rows(db, app_pool):
    _live(db, "clk", ["a", "b"])
    old = _removed(db, source="clk", source_ref="old", days=10)
    purging = _removed(db, source="clk", source_ref="gone", reason="purge", days=3)
    db.commit()
    before = (_state(db, old), _state(db, purging))
    assert await _one(app_pool, wf_all, DELETE, "all", "clk", ["a", "old", "gone"], False, False) == 1
    assert await _one(app_pool, wf_all, DELETE, "all", "clk", ["a", "old", "gone"], False, False) == 0
    assert (_state(db, old), _state(db, purging)) == before


async def test_delete_records_refuses_more_than_500(db, app_pool):
    for i in range(501):
        add_record(db, source="cap", source_ref=f"p{i:03d}")
    db.commit()
    exc = await _fails(app_pool, wf_all, DELETE, "all", "cap", ["p"], True, True)
    assert exc.sqlstate == "P0K0E"
    assert _hidden_refs(db, "cap") == set()


async def test_delete_records_shrink_guard_and_force(db, app_pool):
    refs = [f"r{i:03d}" for i in range(100)]
    _live(db, "shr", refs)
    db.commit()
    exc = await _fails(app_pool, wf_all, DELETE, "all", "shr", refs[:60], False, False)
    assert exc.sqlstate == "P0K10"
    assert exc.diag.message_primary == "shrink 60 50"
    assert _hidden_refs(db, "shr") == set()
    assert await _one(app_pool, wf_all, DELETE, "all", "shr", refs[:60], False, True) == 60
    assert len(_hidden_refs(db, "shr")) == 60


async def test_delete_records_refuses_dataset_source_and_foreign_group(db, app_pool):
    add_record(db, audience="hr", source="hrsrc", source_ref="a")
    db.commit()
    exc = await _fails(app_pool, wf_all, DELETE, "all", "dataset:x", ["a"], False, False)
    assert exc.sqlstate == "P0K0B"
    assert await _one(app_pool, wf_all, DELETE, "hr", "hrsrc", ["a"], False, False) is None
    assert _hidden_refs(db, "hrsrc") == set()


async def test_restore_records_filters(db, app_pool):
    refs = _live(db, "rst", ["one", "grp@1#p1of2", "grp@1#p2of2", "old", "new", "x1", "x2"])
    purging = _removed(db, source="rst", source_ref="purging", reason="purge", days=2)
    db.commit()
    names = ["one", "grp@1#p1of2", "grp@1#p2of2", "old", "new", "x1", "x2"]
    assert await _one(app_pool, wf_all, DELETE, "all", "rst", names, False, True) == 7
    db.execute("UPDATE kh.records SET outdated_at = now() - interval '10 days' WHERE id = %s", (refs["old"],))
    db.commit()

    assert await _one(app_pool, wf_all, RESTORE, "all", "rst", ["one"], False, None) == 1
    assert _state(db, refs["one"])[:2] == (False, "")
    assert _state(db, refs["one"])[3] == ""

    assert await _one(app_pool, wf_all, RESTORE, "all", "rst", ["grp@1"], True, None) == 2

    since = _long_ago(1)
    assert await _one(app_pool, wf_all, RESTORE, "all", "rst", ["old", "new"], False, since) == 1
    assert _state(db, refs["new"])[0] is False and _state(db, refs["old"])[0] is True

    assert await _one(app_pool, wf_all, RESTORE, "all", "rst", None, False, None) == 3
    assert _hidden_refs(db, "rst") == {"purging"}
    assert _state(db, purging)[:2] == (True, "purge")
    assert await _one(app_pool, wf_all, RESTORE, "hr", "rst", None, False, None) is None


async def test_mark_record_outdated_takes_over_and_refuses_purging(db, app_pool):
    rec = add_record(db, source="mk", source_ref="a")
    purging = _removed(db, source="mk", source_ref="p", reason="purge")
    db.commit()
    assert await _one(app_pool, wf_all, DELETE, "all", "mk", ["a"], False, False) == 1
    assert _state(db, rec)[1] == "delete"
    got = await _one(app_pool, alice_all, "SELECT kh.mark_record_outdated(%s, true, '', 'mine')", rec)
    assert got is True
    state = _state(db, rec)
    assert (state[0], state[1], state[3]) == (True, "", "mine")

    exc = await _fails(app_pool, alice_all, "SELECT kh.mark_record_outdated(%s, false, '', '')", purging)
    assert exc.sqlstate == "P0K0F"
    assert _state(db, purging)[:2] == (True, "purge")


async def test_move_record_refuses_removed_record(db, app_pool):
    rec = add_record(db, source="mv", source_ref="a")
    db.commit()
    assert await _one(app_pool, wf_all, DELETE, "all", "mv", ["a"], False, False) == 1
    exc = await _fails(app_pool, multi, "SELECT kh.move_record(%s, 'all', 'mgmt')", rec)
    assert exc.sqlstate == "P0K12"
    assert db.execute("SELECT audience FROM kh.records WHERE id = %s", (rec,)).fetchone() == ("all",)


async def test_purge_claim_respects_the_grace_period(db, indexer_pool):
    due = _removed(db, source="pg", source_ref="due", days=31, blob_key="records/due")
    young = _removed(db, source="pg", source_ref="young", days=1)
    again = _removed(db, source="pg", source_ref="again", reason="purge", days=40, blob_key="records/again")
    skipped = _removed(db, source="pg", source_ref="skipped", reason="purge", days=40)
    live = add_record(db, source="pg", source_ref="live")
    db.commit()

    async def claim(skip: list[str]) -> dict[str, str]:
        async with indexer_pool.connection() as conn:
            cur = await conn.execute(CLAIM, (30, 200, skip))
            return {str(r[0]): r[1] for r in await cur.fetchall()}

    got = await claim([skipped])
    assert got == {due: "records/due", again: "records/again"}
    assert _state(db, due)[1] == "purge"
    assert _state(db, young)[1] == "delete"
    assert _state(db, live)[1] == ""
    got = await claim([again, skipped])
    assert got == {due: "records/due"}
    assert set(await claim([])) == {due, again, skipped}


async def test_purge_claim_rejects_bad_arguments(db, indexer_pool):
    for days, lim in ((0, 200), (30, 0), (30, 1001)):
        with pytest.raises(psycopg.Error) as exc:
            async with indexer_pool.connection() as conn:
                await conn.execute(CLAIM, (days, lim, []))
        assert exc.value.sqlstate == "P0K11"


async def test_purge_delete_removes_rows_chunks_jobs_and_links(db, indexer_pool):
    gone = add_chunked_record(db, "goodbye record", source="pd", source_ref="gone")
    db.execute("UPDATE kh.records SET outdated = true, outdated_at = now() - interval '40 days',"
               " outdated_by = 'x', outdated_reason = 'purge' WHERE id = %s", (gone,))
    pointer = add_record(db, source="pd", source_ref="pointer", outdated=True, outdated_at=_long_ago(1),
                         outdated_by="x", replaced_by=f"rec:{gone}")
    held = add_chunked_record(db, "still here", source="pd", source_ref="held")
    db.execute("UPDATE kh.records SET outdated = true, outdated_at = now() - interval '40 days',"
               " outdated_by = 'x', outdated_reason = 'delete' WHERE id = %s", (held,))
    db.commit()

    def jobs(rec: str) -> int:
        return db.execute(
            "SELECT count(*) FROM kh.index_jobs WHERE kind = 'embed_record' AND payload ->> 'id' = %s", (rec,)
        ).fetchone()[0]

    db.commit()
    assert jobs(gone) == 1 and jobs(held) == 1
    async with indexer_pool.connection() as conn:
        cur = await conn.execute(PURGE_DELETE, ([uuid.UUID(gone), uuid.UUID(held)],))
        assert (await cur.fetchone())[0] == 1
    db.commit()
    assert db.execute("SELECT count(*) FROM kh.records WHERE id = %s", (gone,)).fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM kh.chunks WHERE record_id = %s", (gone,)).fetchone() == (0,)
    assert jobs(gone) == 0
    query = "SELECT replaced_by, outdated FROM kh.records WHERE id = %s"
    assert db.execute(query, (pointer,)).fetchone() == ("", True)
    assert db.execute("SELECT count(*) FROM kh.records WHERE id = %s", (held,)).fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM kh.chunks WHERE record_id = %s", (held,)).fetchone() == (1,)
    assert jobs(held) == 1


def test_purge_functions_are_for_the_indexer_only(db):
    def can(role: str, sig: str) -> bool:
        return db.execute(
            "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')", (role, f"kh.{sig}")
        ).fetchone()[0]

    purge = ("records_purge_claim(integer, integer, uuid[])", "records_purge_delete(uuid[])")
    removal = ("delete_records(text, text, text[], boolean, boolean)",
               "restore_records(text, text, text[], boolean, timestamptz)")
    for sig in purge:
        assert [can(r, sig) for r in ("public", "kh_app", "kh_indexer")] == [False, False, True]
    for sig in removal:
        assert [can(r, sig) for r in ("public", "kh_app", "kh_indexer")] == [False, True, False]
