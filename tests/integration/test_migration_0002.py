"""Migration 0003 (supersede): new columns, the blob_sha reset and the mark_record_outdated definer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import errors

from knowledge_hub.db.migrate import migrate

BASELINE = "0001_baseline.sql"
USAGE = "0002_usage_events.sql"
SUPERSEDE = "0003_supersede.sql"
TECH_TIER = "0004_tech_tier.sql"
MOVE = "0005_move_record.sql"
DB = "kh_fresh"
SUB = "n8n-token:a"
NOW = datetime.now(UTC)


def uid() -> str:
    return str(uuid.uuid4())


def role_conn(cluster, role: str, audiences: str = "", sub: str | None = SUB, ptype: str = "n8n_workflow"):
    conn = psycopg.connect(cluster.dsn(role, DB))
    conn.execute(
        "SELECT set_config('kh.audiences', %s, false), set_config('kh.principal_type', %s, false)",
        (audiences, ptype),
    )
    if sub is not None:
        conn.execute("SELECT set_config('kh.sub', %s, false)", (sub,))
    return conn


def attempt(conn, query, params=()):
    """Run in a savepoint that is rolled back; return the rows (or rowcount) or the error class."""

    class _Rollback(Exception):
        pass

    try:
        with conn.transaction():
            cur = conn.execute(query, params)
            out = cur.fetchall() if cur.description else cur.rowcount
            raise _Rollback
    except _Rollback:
        return out
    except psycopg.Error as exc:
        return type(exc)


def _record(conn, audience="all") -> str:
    rid = uid()
    conn.execute(
        "INSERT INTO kh.records (id, audience, source, source_ref, title, blob_key, excerpt, content_type,"
        " occurred_at, written_by, created_by_sub, created_by_type)"
        " VALUES (%s, %s, 'meet', %s, 't', 'records/x', 'e', 'text/plain', %s, 'kh-bot', %s, 'n8n_workflow')",
        (rid, audience, uid(), NOW, SUB))
    return rid


def _document(conn, audience="all", blob_sha="b") -> str:
    did = uid()
    conn.execute(
        "INSERT INTO kh.documents (id, repo, path, doc_key, audience, title, body, review_by, commit_sha,"
        " blob_sha) VALUES (%s, %s, %s, %s, %s, 't', 'b', current_date, 'c', %s)",
        (did, f"kb-{audience}", f"{did}.md", did, audience, blob_sha))
    return did


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all'), ('mgmt', 'kb-mgmt')")
        yield conn


def _state(admin, rid):
    return admin.execute(
        "SELECT outdated, outdated_at IS NOT NULL, outdated_by, outdated_note, replaced_by"
        " FROM kh.records WHERE id = %s", (rid,)).fetchone()


def test_upgrade_from_baseline_adds_columns_and_resets_blob_sha(migrated_db):
    with psycopg.connect(migrated_db.admin_dsn(), autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS kh_upgrade WITH (FORCE)")
        conn.execute("CREATE DATABASE kh_upgrade OWNER kh_migrator")
    with psycopg.connect(migrated_db.admin_dsn("kh_upgrade"), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")
    dsn = migrated_db.dsn("kh_migrator", "kh_upgrade")
    assert migrate(dsn, stop_after=USAGE) == [BASELINE, USAGE]
    with psycopg.connect(migrated_db.admin_dsn("kh_upgrade"), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all')")
        did = _document(conn, blob_sha="b1")
        rid = _record(conn)
    assert migrate(dsn) == [SUPERSEDE, TECH_TIER, MOVE]
    with psycopg.connect(migrated_db.admin_dsn("kh_upgrade")) as conn:
        assert conn.execute("SELECT supersedes, superseded_by, blob_sha FROM kh.documents WHERE id = %s",
                            (did,)).fetchone() == ([], None, "")
        assert conn.execute(
            "SELECT outdated, outdated_at, outdated_by, outdated_note, replaced_by FROM kh.records"
            " WHERE id = %s", (rid,)).fetchone() == (False, None, "", "", "")


def test_mark_function_sets_and_clears_outdated(admin, fresh_db):
    rid, succ = _record(admin), _record(admin)
    with role_conn(fresh_db, "kh_app", "all") as app:
        assert app.execute("SELECT kh.mark_record_outdated(%s, true, %s, 'old')",
                           (rid, f"rec:{succ}")).fetchone() == (True,)
        app.commit()
    assert _state(admin, rid) == (True, True, SUB, "old", f"rec:{succ}")
    with role_conn(fresh_db, "kh_app", "all") as app:
        assert app.execute("SELECT kh.mark_record_outdated(%s, false, '', '')", (rid,)).fetchone() == (True,)
        app.commit()
    assert _state(admin, rid) == (False, False, "", "", "")


def test_mark_function_answers_null_for_missing_hidden_or_anonymous(admin, fresh_db):
    hidden, visible = _record(admin, "mgmt"), _record(admin)
    with role_conn(fresh_db, "kh_app", "all") as app:
        for rec in (uid(), hidden):
            row = app.execute("SELECT kh.mark_record_outdated(%s, true, '', '')", (rec,)).fetchone()
            assert row == (None,)
    with role_conn(fresh_db, "kh_app", "all", sub=None) as anon:
        row = anon.execute("SELECT kh.mark_record_outdated(%s, true, '', '')", (visible,)).fetchone()
        assert row == (None,)
    assert _state(admin, hidden)[0] is False
    assert _state(admin, visible)[0] is False


def test_mark_function_refuses_foreign_or_self_successor(admin, fresh_db):
    rid = _record(admin)
    foreign = _record(admin, "mgmt")
    foreign_doc = _document(admin, "mgmt")
    bad = [f"rec:{foreign}", f"doc:{foreign_doc}", f"rec:{uid()}", f"doc:{uid()}", "rec:nope", "garbage",
           f"rec:{rid}"]
    with role_conn(fresh_db, "kh_app", "all,mgmt") as app:
        for successor in bad:
            with pytest.raises(psycopg.Error) as exc, app.transaction():
                app.execute("SELECT kh.mark_record_outdated(%s, true, %s, '')", (rid, successor))
            assert exc.value.sqlstate == "P0K07", successor
    assert _state(admin, rid)[0] is False


def test_a_document_can_be_the_successor(admin, fresh_db):
    rid, did = _record(admin), _document(admin)
    with role_conn(fresh_db, "kh_app", "all") as app:
        assert app.execute("SELECT kh.mark_record_outdated(%s, true, %s, '')",
                           (rid, f"doc:{did}")).fetchone() == (True,)
        app.commit()
    assert _state(admin, rid)[4] == f"doc:{did}"


def test_outdated_check_constraint(admin):
    rid = _record(admin)
    assert attempt(admin, "UPDATE kh.records SET replaced_by = 'rec:x' WHERE id = %s", (rid,)) \
        is errors.CheckViolation


def test_kh_app_still_cannot_update_records(admin, fresh_db):
    rid = _record(admin)
    with role_conn(fresh_db, "kh_app", "all") as app:
        assert attempt(app, "UPDATE kh.records SET outdated = true WHERE id = %s", (rid,)) \
            is errors.InsufficientPrivilege
