"""Baseline 0001: forced RLS, the grant and policy matrix and the definer functions (3.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors

DB = "kh_fresh"
SUB = "n8n-token:a"
NOW = datetime.now(UTC)
TABLES = ("schema_migrations", "claim_jti", "groups", "labels", "documents", "records", "chunks",
          "index_jobs", "repo_state", "index_alerts", "usage_events", "record_moves")

# role -> table -> commands granted (a role or table not listed has no grant)
ALL = {"SELECT", "INSERT", "UPDATE", "DELETE"}
GRANTS = {
    "kh_app": {
        "groups": {"SELECT"}, "labels": {"SELECT"}, "documents": {"SELECT"},
        "records": {"SELECT", "INSERT"}, "chunks": {"SELECT"}, "usage_events": {"SELECT", "INSERT"},
    },
    "kh_indexer": {
        "groups": {"SELECT"}, "labels": {"SELECT"}, "documents": ALL, "records": {"SELECT"},
        "chunks": ALL, "index_jobs": ALL, "repo_state": ALL, "index_alerts": ALL,
        "claim_jti": {"SELECT", "DELETE"}, "usage_events": {"SELECT", "DELETE"},
    },
}
DEFINERS = ("claim_seen", "records_enqueue_embed", "labels_in_use_guard", "mark_record_outdated",
            "move_record", "record_moved")
HELPERS = ("caller_audiences", "caller_sub", "caller_type", "can_read")


def uid() -> str:
    return str(uuid.uuid4())


def role_conn(cluster, role: str, audiences: str = "", sub: str = SUB, ptype: str = "n8n_workflow"):
    conn = psycopg.connect(cluster.dsn(role, DB))
    conn.execute(
        "SELECT set_config('kh.audiences', %s, false), set_config('kh.principal_type', %s, false),"
        " set_config('kh.sub', %s, false)",
        (audiences, ptype, sub),
    )
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


def record_row(audience="all", sub=SUB, **over):
    row = dict(id=uid(), audience=audience, source="meet", source_ref=uid(), title="t",
               blob_key="records/x", excerpt="e", content_type="text/plain", occurred_at=NOW,
               written_by="kh-bot", created_by_sub=sub, created_by_type="n8n_workflow")
    row.update(over)
    return row


def insert_sql(table: str, row: dict):
    cols = list(row)
    return (f"INSERT INTO kh.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
            list(row.values()))


# --------------------------------------------------------------------------- catalog


def test_every_table_forces_rls_and_has_migrator_policy(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB)) as conn:
        rows = conn.execute(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
            " EXISTS (SELECT 1 FROM pg_policies p WHERE p.schemaname = 'kh'"
            "   AND p.tablename = c.relname AND p.policyname = 'kh_migrator_all'"
            "   AND p.cmd = 'ALL' AND p.roles = '{kh_migrator}' AND p.qual = 'true'"
            "   AND p.with_check = 'true')"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'kh' AND c.relkind IN ('r', 'p')"
        ).fetchall()
        assert {r[0] for r in rows} == set(TABLES)
        for name, rls, force, migrator in rows:
            assert rls and force and migrator, name
        multi = conn.execute(
            "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'kh'"
            " AND cardinality(roles) <> 1"
        ).fetchall()
        assert multi == []
        dup = conn.execute(
            "SELECT tablename, roles, cmd FROM pg_policies WHERE schemaname = 'kh'"
            " GROUP BY 1, 2, 3 HAVING count(*) > 1"
        ).fetchall()
        assert dup == []


def test_policy_texts(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB)) as conn:
        rows = conn.execute(
            "SELECT tablename, policyname, cmd, qual, with_check FROM pg_policies"
            " WHERE schemaname = 'kh' AND policyname <> 'kh_migrator_all'"
        ).fetchall()
    by = {(t, p): (cmd, q, w) for t, p, cmd, q, w in rows}
    assert by[("documents", "kh_app_select")] == ("SELECT", "kh.can_read(audience)", None)
    assert by[("chunks", "kh_app_select")] == ("SELECT", "kh.can_read(audience)", None)
    assert by[("records", "kh_app_select")] == ("SELECT", "kh.can_read(audience)", None)
    cmd, q, check = by[("records", "kh_app_insert")]
    assert cmd == "INSERT" and q is None
    assert "kh.can_read(audience)" in check and "created_by_sub = kh.caller_sub()" in check
    for t in ("groups", "labels"):
        assert by[(t, "kh_app_select")][1] == "true"
        assert by[(t, "kh_indexer_select")][1] == "true"
    assert by[("records", "kh_indexer_select")][1] == "true"
    for t in ("documents", "chunks", "index_jobs", "repo_state", "index_alerts"):
        assert by[(t, "kh_indexer_all")][0] == "ALL"
    select_qual = by[("usage_events", "kh_app_select")][1]
    assert "cardinality(audiences) > 0" in select_qual and "<@ kh.caller_audiences()" in select_qual
    insert_check = by[("usage_events", "kh_app_insert")][2]
    assert "audiences = kh.caller_audiences()" in insert_check
    assert "kh.caller_type()" in insert_check and "claude_client" in insert_check
    assert by[("usage_events", "kh_indexer_select")][1] == "true"
    assert by[("usage_events", "kh_indexer_delete")][1] == "true"


def test_grant_matrix(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB)) as conn:
        for role in ("kh_app", "kh_indexer"):
            for table in TABLES:
                for priv in sorted(ALL):
                    got = conn.execute(
                        "SELECT has_table_privilege(%s, %s, %s)", (role, f"kh.{table}", priv)
                    ).fetchone()[0]
                    want = priv in GRANTS[role].get(table, set())
                    assert got is want, (role, table, priv)
                for priv in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                    q = "SELECT has_table_privilege(%s, %s, %s)"
                    assert not conn.execute(q, (role, f"kh.{table}", priv)).fetchone()[0], (
                        role, table, priv)


def test_roles_are_plain_and_there_are_exactly_three(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB)) as conn:
        rows = conn.execute(
            "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin FROM pg_roles"
            " WHERE rolname LIKE 'kh\\_%' ORDER BY 1"
        ).fetchall()
    assert rows == [("kh_app", False, False, True), ("kh_indexer", False, False, True),
                    ("kh_migrator", False, False, True)]


def test_definers_are_owned_by_migrator_with_a_pinned_search_path(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB)) as conn:
        for fn in DEFINERS:
            rows = conn.execute(
                "SELECT pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'kh'"
                " AND p.proname = %s", (fn,)).fetchall()
            assert rows == [("kh_migrator", True, ["search_path=kh, pg_temp"])], fn
        for fn in ("claim_seen",):
            q = ("SELECT has_function_privilege(%s, p.oid, 'EXECUTE') FROM pg_proc p"
                 " WHERE p.proname = %s AND p.pronamespace = 'kh'::regnamespace")
            assert conn.execute(q, ("kh_app", fn)).fetchone()[0]
            assert not conn.execute(q, ("kh_indexer", fn)).fetchone()[0]
        for fn in HELPERS:
            q = ("SELECT p.prosecdef, has_function_privilege(%s, p.oid, 'EXECUTE') FROM pg_proc p"
                 " WHERE p.proname = %s AND p.pronamespace = 'kh'::regnamespace")
            assert conn.execute(q, ("kh_app", fn)).fetchone() == (False, True), fn
            assert conn.execute(q, ("kh_indexer", fn)).fetchone() == (False, False), fn


# --------------------------------------------------------------------------- behaviour


def test_labels_and_groups_are_readable_by_both_roles(seeded_db, fresh_db):
    for role, aud in (("kh_app", ""), ("kh_app", "mgmt"), ("kh_indexer", "")):
        with role_conn(fresh_db, role, aud) as conn:
            assert {r[0] for r in conn.execute("SELECT name FROM kh.groups")} == {"all", "mgmt", "hr"}
            assert {r[0] for r in conn.execute("SELECT name FROM kh.labels")} == {"ember", "other"}


def test_indexer_reads_every_document_record_and_chunk(seeded_db, fresh_db):
    with role_conn(fresh_db, "kh_indexer") as conn:
        for table in ("documents", "records"):
            assert conn.execute(f"SELECT count(*) FROM kh.{table}").fetchone() == (3,)
        assert conn.execute("SELECT count(*) FROM kh.chunks").fetchone() == (6,)


def test_indexer_writes_documents_and_chunks(seeded_db, fresh_db):
    with role_conn(fresh_db, "kh_indexer") as conn:
        doc = seeded_db["documents"]["all"]
        assert attempt(conn, "UPDATE kh.documents SET title = 'x' WHERE id = %s", [doc]) == 1
        assert attempt(conn, "DELETE FROM kh.chunks") == 6
        assert attempt(conn, "DELETE FROM kh.documents") == 3
        assert attempt(conn, "INSERT INTO kh.index_jobs (kind) VALUES ('git_push')") == 1


def test_indexer_cannot_write_records_or_groups(seeded_db, fresh_db):
    with role_conn(fresh_db, "kh_indexer") as conn:
        assert attempt(conn, *insert_sql("records", record_row())) is errors.InsufficientPrivilege
        assert attempt(conn, "UPDATE kh.records SET title = 'x'") is errors.InsufficientPrivilege
        assert attempt(conn, "DELETE FROM kh.labels") is errors.InsufficientPrivilege
        assert attempt(conn, "INSERT INTO kh.groups (name, repo) VALUES ('z', 'kb-z')") \
            is errors.InsufficientPrivilege


@pytest.mark.parametrize("table", ["index_jobs", "repo_state", "index_alerts", "claim_jti",
                                   "schema_migrations", "record_moves"])
def test_app_has_no_access_to_internal_tables(seeded_db, fresh_db, table):
    with role_conn(fresh_db, "kh_app", "all") as conn:
        assert attempt(conn, f"SELECT * FROM kh.{table}") is errors.InsufficientPrivilege


@pytest.mark.parametrize("table", ["documents", "records", "chunks", "groups", "labels"])
def test_app_cannot_update_or_delete(seeded_db, fresh_db, table):
    with role_conn(fresh_db, "kh_app", "all,mgmt,hr") as conn:
        for stmt in (f"UPDATE kh.{table} SET description = description" if table == "labels"
                     else f"UPDATE kh.{table} SET audience = audience" if table != "groups"
                     else "UPDATE kh.groups SET repo = repo", f"DELETE FROM kh.{table}"):
            assert attempt(conn, stmt) is errors.InsufficientPrivilege, stmt


def test_claim_jti_is_only_purged_when_expired(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.claim_jti VALUES ('old', %s), ('new', %s)",
                     [NOW - timedelta(hours=1), NOW + timedelta(hours=1)])
    with role_conn(fresh_db, "kh_indexer") as conn:
        assert [r[0] for r in conn.execute("SELECT jti FROM kh.claim_jti")] == ["old"]
        assert attempt(conn, "DELETE FROM kh.claim_jti") == 1
        assert attempt(conn, "INSERT INTO kh.claim_jti VALUES ('x', now())") \
            is errors.InsufficientPrivilege


def test_claim_seen_is_the_only_door_for_the_app(fresh_db):
    with role_conn(fresh_db, "kh_app") as conn:
        exp = NOW + timedelta(minutes=5)
        assert conn.execute("SELECT kh.claim_seen('j1', %s)", [exp]).fetchone() == (True,)
        assert conn.execute("SELECT kh.claim_seen('j1', %s)", [exp]).fetchone() == (False,)


# --------------------------------------------------------------------------- audiences


@pytest.mark.parametrize(("held", "visible"), [
    ("", set()),
    ("all", {"all"}),
    ("mgmt", {"mgmt"}),
    ("all,mgmt", {"all", "mgmt"}),
    ("all,mgmt,hr", {"all", "mgmt", "hr"}),
    ("nope", set()),
])
def test_reads_follow_held_audiences(seeded_db, fresh_db, held, visible):
    with role_conn(fresh_db, "kh_app", held) as conn:
        for table in ("documents", "records"):
            got = {r[0] for r in conn.execute(f"SELECT audience FROM kh.{table}")}
            assert got == visible, table
        assert {r[0] for r in conn.execute("SELECT DISTINCT audience FROM kh.chunks")} == visible


def test_guessed_id_of_unheld_row_returns_nothing(seeded_db, fresh_db):
    with role_conn(fresh_db, "kh_app", "all") as conn:
        for table, key in (("documents", "documents"), ("records", "records"), ("chunks", "chunks")):
            hidden = seeded_db[key]["hr"]
            assert conn.execute(f"SELECT count(*) FROM kh.{table} WHERE id = %s", [hidden]).fetchone() == (0,)


def test_can_read_is_false_for_null_and_unset(fresh_db):
    with role_conn(fresh_db, "kh_app", "all") as conn:
        assert conn.execute("SELECT kh.can_read('all'), kh.can_read('hr'), kh.can_read(NULL)").fetchone() \
            == (True, False, False)
    with role_conn(fresh_db, "kh_app", "") as conn:
        assert conn.execute("SELECT kh.can_read('all')").fetchone() == (False,)


# --------------------------------------------------------------------------- inserting records


def test_insert_needs_the_audience(seeded_db, fresh_db):
    with role_conn(fresh_db, "kh_app", "all") as conn:
        assert attempt(conn, *insert_sql("records", record_row("all"))) == 1
        assert attempt(conn, *insert_sql("records", record_row("hr"))) is errors.InsufficientPrivilege
    with role_conn(fresh_db, "kh_app", "") as conn:
        assert attempt(conn, *insert_sql("records", record_row("all"))) is errors.InsufficientPrivilege


def test_insert_needs_created_by_sub_to_be_the_caller(seeded_db, fresh_db):
    with role_conn(fresh_db, "kh_app", "all", sub="n8n-token:me") as conn:
        assert attempt(conn, *insert_sql("records", record_row(sub="n8n-token:me"))) == 1
        assert attempt(conn, *insert_sql("records", record_row(sub="n8n-token:other"))) \
            is errors.InsufficientPrivilege


def test_insert_without_caller_sub_is_refused(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_app", DB)) as conn:
        conn.execute("SELECT set_config('kh.audiences', 'all', false)")
        assert attempt(conn, *insert_sql("records", record_row())) is errors.InsufficientPrivilege


def test_insert_of_unknown_group_is_a_policy_or_fk_error(seeded_db, fresh_db):
    with role_conn(fresh_db, "kh_app", "ghost") as conn:
        assert attempt(conn, *insert_sql("records", record_row("ghost"))) is errors.ForeignKeyViolation


def test_duplicate_insert_hits_the_unique_key_and_a_select_finds_the_row(seeded_db, fresh_db):
    row = record_row("all", source="m", source_ref="r1")
    with role_conn(fresh_db, "kh_app", "all") as conn:
        conn.execute(*insert_sql("records", row))
        again = record_row("all", source="m", source_ref="r1")
        assert attempt(conn, *insert_sql("records", again)) is errors.UniqueViolation
        found = conn.execute(
            "SELECT id FROM kh.records WHERE audience = 'all' AND source = 'm' AND source_ref = 'r1'"
        ).fetchall()
        assert found == [(uuid.UUID(row["id"]),)]


def test_same_source_ref_in_another_group_is_not_a_duplicate(seeded_db, fresh_db):
    with role_conn(fresh_db, "kh_app", "all,mgmt") as conn:
        conn.execute(*insert_sql("records", record_row("all", source="m", source_ref="r1")))
        conn.execute(*insert_sql("records", record_row("mgmt", source="m", source_ref="r1")))


def test_a_reader_of_another_group_never_sees_the_duplicate(seeded_db, fresh_db):
    with role_conn(fresh_db, "kh_app", "mgmt") as conn:
        got = conn.execute("SELECT count(*) FROM kh.records WHERE source = 'seed' AND source_ref = 'ref-all'")
        assert got.fetchone() == (0,)


def test_mark_record_outdated_is_not_executable_by_public_or_indexer(fresh_db):
    q = ("SELECT has_function_privilege(%s, p.oid, 'EXECUTE') FROM pg_proc p"
         " WHERE p.proname = 'mark_record_outdated' AND p.pronamespace = 'kh'::regnamespace")
    with psycopg.connect(fresh_db.admin_dsn(DB)) as conn:
        assert conn.execute(q, ("kh_app",)).fetchone()[0]
        assert not conn.execute(q, ("kh_indexer",)).fetchone()[0]
        assert conn.execute("SELECT count(*) FROM pg_proc p, aclexplode(p.proacl) a"
                            " WHERE p.proname = 'mark_record_outdated' AND a.grantee = 0").fetchone() == (0,)
