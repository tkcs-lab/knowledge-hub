"""Migration 0005 (move_record): the move and tombstone definer functions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
import pytest

DB = "kh_fresh"
SUB = "multi"
NOW = datetime.now(UTC)


def uid() -> str:
    return str(uuid.uuid4())


def role_conn(cluster, role: str, audiences: str = "", sub: str | None = SUB, ptype: str = "claude_client"):
    conn = psycopg.connect(cluster.dsn(role, DB))
    conn.execute(
        "SELECT set_config('kh.audiences', %s, false), set_config('kh.principal_type', %s, false)",
        (audiences, ptype),
    )
    if sub is not None:
        conn.execute("SELECT set_config('kh.sub', %s, false)", (sub,))
    return conn


def _record(conn, audience="all", source_ref=None) -> str:
    rid = uid()
    conn.execute(
        "INSERT INTO kh.records (id, audience, source, source_ref, title, blob_key, excerpt, content_type,"
        " occurred_at, written_by, created_by_sub, created_by_type)"
        " VALUES (%s, %s, 'meet', %s, 't', 'records/x', 'e', 'text/plain', %s, 'kh-bot', %s,"
        " 'claude_client')",
        (rid, audience, source_ref or uid(), NOW, SUB))
    return rid


def _chunk(conn, rid, audience="all"):
    conn.execute(
        "INSERT INTO kh.chunks (record_id, ordinal, heading_path, text, content_sha, tier)"
        " VALUES (%s, 0, '', 'hello', 'sha', 'T3')", (rid,))


def move(conn, rid, from_aud, to_aud):
    return conn.execute("SELECT kh.move_record(%s, %s, %s)", (rid, from_aud, to_aud)).fetchone()[0]


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all'), ('mgmt', 'kb-mgmt'),"
                     " ('hr', 'kb-hr')")
        yield conn


def test_move_function_moves_record_and_chunks(admin, fresh_db):
    rid = _record(admin)
    _chunk(admin, rid)
    admin.execute("UPDATE kh.records SET outdated = true, outdated_at = now(), outdated_by = 'x',"
                  " outdated_note = 'old', replaced_by = %s WHERE id = %s", (f"rec:{uid()}", rid))
    with role_conn(fresh_db, "kh_app", "all,mgmt") as conn:
        assert move(conn, rid, "all", "mgmt") == "all"
        conn.commit()
    assert admin.execute("SELECT audience, replaced_by, outdated, outdated_note FROM kh.records"
                         " WHERE id = %s", (rid,)).fetchone() == ("mgmt", "", True, "old")
    assert admin.execute("SELECT audience FROM kh.chunks WHERE record_id = %s", (rid,)).fetchall() \
        == [("mgmt",)]


def test_move_function_answers_null_for_missing_hidden_or_anonymous(admin, fresh_db):
    hidden = _record(admin, "hr")
    mine = _record(admin)
    with role_conn(fresh_db, "kh_app", "all,mgmt") as conn:
        assert move(conn, uid(), "all", "mgmt") is None
        assert move(conn, hidden, "hr", "mgmt") is None
    with role_conn(fresh_db, "kh_app", "all,mgmt", sub=None) as conn:
        assert move(conn, mine, "all", "mgmt") is None


def test_move_function_refuses_unheld_target(admin, fresh_db):
    rid = _record(admin)
    with role_conn(fresh_db, "kh_app", "all") as conn, pytest.raises(psycopg.Error) as exc:
        move(conn, rid, "all", "mgmt")
    assert exc.value.sqlstate == "P0K08"


def test_move_function_refuses_same_group_and_wrong_from(admin, fresh_db):
    rid = _record(admin)
    for args in (("all", "all"), ("mgmt", "all")):
        with role_conn(fresh_db, "kh_app", "all,mgmt") as conn, pytest.raises(psycopg.Error) as exc:
            move(conn, rid, *args)
        assert exc.value.sqlstate == "P0K09"


def test_move_function_refuses_linked_record(admin, fresh_db):
    rid, other = _record(admin), _record(admin)
    admin.execute("UPDATE kh.records SET outdated = true, replaced_by = %s WHERE id = %s",
                  (f"rec:{rid}", other))
    with role_conn(fresh_db, "kh_app", "all,mgmt") as conn, pytest.raises(psycopg.Error) as exc:
        move(conn, rid, "all", "mgmt")
    assert exc.value.sqlstate == "P0K0A"


def test_move_function_writes_tombstone_and_clears_it_on_move_back(admin, fresh_db):
    rid = _record(admin, source_ref="ref-1")
    with role_conn(fresh_db, "kh_app", "all,mgmt") as conn:
        move(conn, rid, "all", "mgmt")
        conn.commit()
    assert admin.execute("SELECT audience, source, source_ref FROM kh.record_moves").fetchall() \
        == [("all", "meet", "ref-1")]
    with role_conn(fresh_db, "kh_app", "all,mgmt") as conn:
        move(conn, rid, "mgmt", "all")
        conn.commit()
    assert admin.execute("SELECT audience FROM kh.record_moves").fetchall() == [("mgmt",)]


def test_move_function_queues_embed_job(admin, fresh_db):
    rid = _record(admin)
    admin.execute("DELETE FROM kh.index_jobs")
    with role_conn(fresh_db, "kh_app", "all,mgmt") as conn:
        move(conn, rid, "all", "mgmt")
        conn.commit()
    assert admin.execute("SELECT kind, payload->>'id' FROM kh.index_jobs").fetchall() \
        == [("embed_record", rid)]


def test_record_moved_is_false_for_unheld_group(admin, fresh_db):
    rid = _record(admin, source_ref="ref-2")
    with role_conn(fresh_db, "kh_app", "all,mgmt") as conn:
        move(conn, rid, "all", "mgmt")
        conn.commit()
    check = "SELECT kh.record_moved('all', 'meet', 'ref-2')"
    with role_conn(fresh_db, "kh_app", "all") as conn:
        assert conn.execute(check).fetchone()[0] is True
    with role_conn(fresh_db, "kh_app", "mgmt") as conn:
        assert conn.execute(check).fetchone()[0] is False
