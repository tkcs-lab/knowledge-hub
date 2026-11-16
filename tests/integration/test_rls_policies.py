"""Migration 0003: helper and definer functions, RLS on every table and the policy matrix (3.3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors, sql

DB = "kh_fresh"
SUB_A = "n8n-token:a"
SUB_B = "n8n-token:b"
VIS = ("required_audiences", "allowed_emails")
SNAP = ("src_required_audiences", "src_allowed_emails")

# Literal copy of the matrix in plan section 3.3 (a role not listed has no grant).
# select: "visible" (kh_app policy), "parent" (visible parent, EXISTS under RLS), "all" (true),
#         "expired" (expires_at < now()), or a tuple of granted columns (policy true).
# insert: "check" (kh_app WITH CHECK, see INSERT_CHECK) or "all".
# update: "all" or a tuple of granted columns.  delete: "all" or "expired".
ALL = {"select": "all", "insert": "all", "update": "all", "delete": "all"}
MATRIX: dict[str, dict[str, dict[str, object]]] = {
    "documents": {
        "kh_app": {"select": "visible"},
        "kh_indexer": ALL,
        "kh_provenance": {"select": ("id", "required_audiences", "allowed_emails", "commit_sha")},
    },
    "document_versions": {"kh_app": {"select": "parent"}, "kh_indexer": ALL},
    "chunks": {"kh_app": {"select": "visible"}, "kh_indexer": ALL},
    "entities_cache": {"kh_app": {"select": "visible"}, "kh_indexer": ALL},
    "raw_records": {
        "kh_app": {"select": "visible", "insert": "check"},
        "kh_indexer": {"select": "all", "update": VIS},
        "kh_provenance": {"select": ("id", "required_audiences", "allowed_emails", "ingested_by_sub")},
    },
    "raw_audience_map": {"kh_indexer": ALL, "kh_provenance": {"select": "all"}},
    "derived_records": {
        "kh_app": {"select": "visible", "insert": "check"},
        "kh_indexer": {"select": "all", "update": VIS},
        "kh_provenance": {"select": ("id", "required_audiences", "allowed_emails", "created_by_sub")},
    },
    # Snapshot-column UPDATE for kh_indexer on the three source tables comes from the
    # prose of 3.3 below the matrix ("kh_indexer may UPDATE those two columns").
    "derived_sources": {
        "kh_app": {"select": "parent", "insert": "check"},
        "kh_indexer": {"select": "all", "update": SNAP},
        "kh_provenance": {"select": "all"},
    },
    # kh_provenance reads the parent and snapshot columns for the 0006 constraint triggers.
    "candidates": {
        "kh_app": {"select": "visible", "insert": "check"},
        "kh_indexer": {"select": "all", "update": ("state", "decided_at") + VIS},
        "kh_provenance": {"select": ("id", "required_audiences", "allowed_emails")},
    },
    "candidate_sources": {
        "kh_app": {"select": "parent", "insert": "check"},
        "kh_indexer": {"select": "all", "update": SNAP},
        "kh_provenance": {"select": "all"},
    },
    "conflicts": {
        "kh_app": {"select": "visible", "insert": "check"},
        "kh_indexer": {"select": "all", "update": ("state", "closed_at", "closed_by_pr") + VIS},
        "kh_provenance": {"select": ("id", "required_audiences", "allowed_emails")},
    },
    "conflict_items": {
        "kh_app": {"select": "parent", "insert": "check"},
        "kh_indexer": {"select": "all", "update": SNAP},
        "kh_provenance": {"select": "all"},
    },
    "index_jobs": {"kh_indexer": ALL},
    "repo_state": {"kh_indexer": ALL},
    "merge_verdicts": {"kh_indexer": ALL},
    "index_alerts": {"kh_indexer": ALL},
    "claim_jti": {"kh_indexer": {"select": "expired", "delete": "expired"}},
    "schema_migrations": {},
}
ROLES = ("kh_app", "kh_indexer", "kh_provenance")
COMMANDS = ("select", "insert", "update", "delete")

# A column every role can be asked to UPDATE (``col = col``); for column grants it is the
# column outside the grant.
PROBE = {
    "documents": "title", "document_versions": "path", "chunks": "text",
    "entities_cache": "name", "raw_records": "title", "raw_audience_map": "audiences",
    "derived_records": "title", "derived_sources": "version", "candidates": "title",
    "candidate_sources": "source_id", "conflicts": "note", "conflict_items": "item_id",
    "index_jobs": "last_error", "repo_state": "config_hash", "index_alerts": "message",
    "merge_verdicts": "reason",
    "claim_jti": "expires_at", "schema_migrations": "sha256",
}

DEFINER_FUNCS = {
    "owns_derived": "kh_migrator", "owns_candidate": "kh_migrator", "owns_conflict": "kh_migrator",
    "my_raw_id": "kh_migrator", "claim_seen": "kh_migrator", "candidate_mark": "kh_migrator",
}
HELPER_FUNCS = ("caller_audiences", "caller_email", "caller_sub", "caller_type", "visible")

NOW = datetime.now(UTC)


def uid() -> str:
    return str(uuid.uuid4())


def factories(p: dict[str, str]) -> dict:
    """Column values for a new row of each table; ``p`` holds the seeded parent ids."""
    vis = {"required_audiences": ["all"], "allowed_emails": None}
    snap = {"src_required_audiences": ["all"], "src_allowed_emails": None}
    return {
        "documents": lambda: {"id": uid(), "repo": "canonical-all", "path": f"p/{uid()}.md",
                              "doc_key": uid(), "tier": "T0", "domain": "product", "title": "t",
                              "body": "b", "status": "current", "commit_sha": "c1",
                              "content_sha": "s1", **vis},
        "document_versions": lambda: {"document_id": p["doc"], "commit_sha": uid(), "path": "p.md",
                                      "tier": "T0", "status": "current"},
        "chunks": lambda: {"id": uid(), "document_id": p["doc"], "ordinal": 0, "text": "hello",
                           "content_sha": "s", "owner_key": f"doc:{p['doc']}", "tier": "T0", **vis},
        "entities_cache": lambda: {"id": f"prd:{uid()}", "kind": "prd", "name": "n", **vis},
        "raw_records": lambda: {"id": uid(), "source_system": "meet", "source_ref": uid(),
                                "ingested_by_sub": SUB_A, "ingested_by_type": "n8n_workflow", **vis},
        "raw_audience_map": lambda: {"kind": "series", "key": uid(), "audiences": ["all"]},
        "derived_records": lambda: {"id": uid(), "kind": "summary", "title": "t", "body": "b",
                                    "created_by_sub": SUB_A, "created_by_type": "n8n_workflow", **vis},
        "derived_sources": lambda: {"derived_id": p["derived"], "source_kind": "raw",
                                    "source_id": uid(), **snap},
        "candidates": lambda: {"id": uid(), "state": "proposed", "domain": "product",
                               "target_repo": "canonical-all", "target_path": "product/x.md",
                               "title": "t", "body_sha": "s", "dedupe_key": uid(),
                               "created_by_sub": SUB_A, **vis},
        "candidate_sources": lambda: {"candidate_id": p["candidate"], "source_kind": "raw",
                                      "source_id": uid(), **snap},
        "conflicts": lambda: {"id": uid(), "state": "open", "created_by_sub": SUB_A, **vis},
        "conflict_items": lambda: {"conflict_id": p["conflict"], "item_kind": "raw",
                                   "item_id": uid(), **snap},
        "index_jobs": lambda: {"kind": "git_push", "repo": "canonical-all"},
        "repo_state": lambda: {"repo": uid()},
        "merge_verdicts": lambda: {"repo": uid(), "merge_sha": uid(), "status": "disputed"},
        "index_alerts": lambda: {"repo": "canonical-all", "message": "m"},
        "claim_jti": lambda: {"jti": uid(), "expires_at": NOW + timedelta(hours=1)},
        "schema_migrations": lambda: {"name": uid(), "sha256": "x"},
    }


def insert(conn: psycopg.Connection, table: str, cols: dict) -> int:
    q = sql.SQL("INSERT INTO kh.{} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols)),
    )
    return conn.execute(q, list(cols.values())).rowcount


def seed_mode(conn):
    """Seed rows verbatim: switch off the 0004 triggers (covered by test_triggers.py)."""
    for t in ("chunks", "raw_records", "derived_records", "derived_sources", "candidates",
              "candidate_sources", "conflicts", "conflict_items"):
        conn.execute(f"ALTER TABLE kh.{t} DISABLE TRIGGER USER")


def set_caller(conn, audiences="all", email="alice@example.org", ptype="n8n_workflow", sub=SUB_A):
    conn.execute(
        "SELECT set_config('kh.audiences', %s, false), set_config('kh.email', %s, false),"
        " set_config('kh.principal_type', %s, false), set_config('kh.sub', %s, false)",
        (audiences, email or "", ptype, sub),
    )


@pytest.fixture
def seeded(fresh_db):
    """Seed one row per table (visibility ``{all}``, created by SUB_A) as the sandbox superuser."""
    p: dict[str, str] = {}
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        seed_mode(conn)
        f = factories(p)
        for table, key in (("documents", "doc"), ("derived_records", "derived"),
                           ("candidates", "candidate"), ("conflicts", "conflict")):
            row = f[table]()
            insert(conn, table, row)
            p[key] = row["id"]
        f = factories(p)
        for table in MATRIX:
            if table not in ("documents", "derived_records", "candidates", "conflicts"):
                insert(conn, table, f[table]())
        insert(conn, "claim_jti", {"jti": "expired-1", "expires_at": NOW - timedelta(hours=1)})
    return fresh_db, p


def role_conn(cluster, role: str) -> psycopg.Connection:
    """kh_provenance is NOLOGIN: reach it from kh_migrator (a member) with SET ROLE."""
    if role == "kh_provenance":
        conn = psycopg.connect(cluster.dsn("kh_migrator", DB))
        conn.execute("SET ROLE kh_provenance")
        return conn
    return psycopg.connect(cluster.dsn(role, DB))


def run(conn, query, params=()):
    """Run in a savepoint; return the cursor result or the exception class."""
    try:
        with conn.transaction():
            cur = conn.execute(query, params)
            out = cur.fetchall() if cur.description else cur.rowcount
            raise _Rollback(out)
    except _Rollback as rb:
        return rb.value
    except psycopg.Error as exc:
        return type(exc)


class _Rollback(Exception):
    def __init__(self, value):
        super().__init__()
        self.value = value


def tables_in_kh(cluster) -> set[str]:
    with psycopg.connect(cluster.admin_dsn(DB)) as conn:
        rows = conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'kh' AND c.relkind IN ('r', 'p')"
        ).fetchall()
    return {r[0] for r in rows}


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
        assert {r[0] for r in rows} == set(MATRIX)
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


def test_no_truncate_references_or_trigger_privileges(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB)) as conn:
        for table in tables_in_kh(fresh_db):
            for role in ROLES:
                for priv in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                    q = "SELECT has_table_privilege(%s, %s, %s)"
                    assert not conn.execute(q, (role, f"kh.{table}", priv)).fetchone()[0], (
                        role, table, priv)


def test_migration_ran_as_kh_migrator_and_definers_are_owned_as_named(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB)) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM kh.schema_migrations ORDER BY name")]
        assert "0003_rls.sql" in names
        sup, bypass = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'kh_migrator'").fetchone()
        assert not sup and not bypass
        owners = {r[0] for r in conn.execute(
            "SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'kh'")}
        assert owners == {"kh_migrator"}
        for fn, owner in DEFINER_FUNCS.items():
            row = conn.execute(
                "SELECT pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'kh'"
                " AND p.proname = %s", (fn,)).fetchall()
            assert len(row) == 1, fn
            assert row[0][0] == owner and row[0][1] is True, fn
            assert row[0][2] == ["search_path=kh, pg_temp"], fn
            oid = f"kh.{fn}"
            q = ("SELECT has_function_privilege(%s, p.oid, 'EXECUTE') FROM pg_proc p"
                 " WHERE p.oid::regproc::text = %s")
            assert conn.execute(q, ("kh_app", oid)).fetchone()[0], fn
            assert not conn.execute(q, ("kh_indexer", oid)).fetchone()[0], fn
            assert not conn.execute(q, ("kh_provenance", oid)).fetchone()[0], fn
        for fn in HELPER_FUNCS:
            oid = f"kh.{fn}"
            q = ("SELECT p.prosecdef, has_function_privilege(%s, p.oid, 'EXECUTE') FROM pg_proc p"
                 " WHERE p.oid::regproc::text = %s")
            assert conn.execute(q, ("kh_app", oid)).fetchone() == (False, True), fn
            assert conn.execute(q, ("kh_provenance", oid)).fetchone() == (False, True), fn
            assert conn.execute(q, ("kh_indexer", oid)).fetchone() == (False, False), fn


# --------------------------------------------------------------------------- matrix


def _check_select(conn, table, spec, role):
    star = sql.SQL("SELECT * FROM kh.{}").format(sql.Identifier(table))
    if spec is None:
        assert run(conn, star) is errors.InsufficientPrivilege, (role, table)
        return
    if isinstance(spec, tuple):
        assert run(conn, star) is errors.InsufficientPrivilege, (role, table)
        cols = sql.SQL("SELECT {} FROM kh.{}").format(
            sql.SQL(", ").join(map(sql.Identifier, spec)), sql.Identifier(table))
        assert len(run(conn, cols)) == 1, (role, table)
        return
    rows = run(conn, star)
    assert isinstance(rows, list), (role, table, rows)
    if spec == "expired":
        assert [r[0] for r in rows] == ["expired-1"], (role, table)
    else:
        assert len(rows) == 1, (role, table, spec)
    if spec in ("visible", "parent"):
        set_caller(conn, audiences="mgmt", email="")
        assert run(conn, star) == [], (role, table, "hidden from another audience")
        set_caller(conn, audiences="", email="", ptype="", sub="")
        assert run(conn, star) == [], (role, table, "hidden without caller settings")
        set_caller(conn)


def _check_insert(conn, table, spec, role, make):
    if spec is None:
        assert run(conn, *_ins(table, make())) is errors.InsufficientPrivilege, (role, table)
        return
    assert run(conn, *_ins(table, make())) == 1, (role, table)
    if spec == "check":
        set_caller(conn, sub=SUB_B)
        assert run(conn, *_ins(table, make())) is errors.InsufficientPrivilege, (role, table)
        set_caller(conn)
        if table == "raw_records":
            set_caller(conn, ptype="claude_client")
            assert run(conn, *_ins(table, make())) is errors.InsufficientPrivilege, (role, table)
            set_caller(conn)


def _ins(table, cols):
    q = sql.SQL("INSERT INTO kh.{} ({}) VALUES ({})").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols)))
    return q, list(cols.values())


def _upd(table, col):
    return sql.SQL("UPDATE kh.{t} SET {c} = {c}").format(t=sql.Identifier(table), c=sql.Identifier(col))


def _check_update(conn, table, spec, role):
    probe = PROBE[table]
    if spec is None:
        assert run(conn, _upd(table, probe)) is errors.InsufficientPrivilege, (role, table)
        return
    if spec == "all":
        assert run(conn, _upd(table, probe)) >= 1, (role, table)
        return
    assert probe not in spec
    assert run(conn, _upd(table, probe)) is errors.InsufficientPrivilege, (role, table)
    for col in spec:
        assert run(conn, _upd(table, col)) >= 1, (role, table, col)


def _check_delete(conn, table, spec, role):
    q = sql.SQL("DELETE FROM kh.{}").format(sql.Identifier(table))
    if spec is None:
        assert run(conn, q) is errors.InsufficientPrivilege, (role, table)
    elif spec == "expired":
        assert run(conn, q) == 1, (role, table)
    else:
        assert run(conn, q) >= 1, (role, table)


@pytest.mark.parametrize("role", ROLES)
def test_policy_matrix(seeded, role):
    cluster, parents = seeded
    f = factories(parents)
    with role_conn(cluster, role) as conn:
        set_caller(conn)
        for table, roles in MATRIX.items():
            spec = roles.get(role, {})
            _check_select(conn, table, spec.get("select"), role)
            _check_insert(conn, table, spec.get("insert"), role, f[table])
            _check_update(conn, table, spec.get("update"), role)
            _check_delete(conn, table, spec.get("delete"), role)


def test_kh_migrator_has_every_command_on_every_table(seeded):
    cluster, parents = seeded
    f = factories(parents)
    with psycopg.connect(cluster.dsn("kh_migrator", DB)) as conn:
        for table in MATRIX:
            assert isinstance(run(conn, sql.SQL("SELECT * FROM kh.{}").format(sql.Identifier(table))),
                              list)
            assert run(conn, *_ins(table, f[table]())) == 1, table
            assert run(conn, _upd(table, PROBE[table])) >= 1, table
            assert run(conn, sql.SQL("DELETE FROM kh.{}").format(sql.Identifier(table))) >= 1, table


# --------------------------------------------------------------------------- functions


VISIBLE_CASES = [
    # (caller audiences, caller email, row audiences, row emails, expected)
    ("all", None, ["all"], None, True),
    ("all", None, ["mgmt"], None, False),
    ("all", None, ["all", "mgmt"], None, False),
    ("all,mgmt", None, ["all", "mgmt"], None, True),
    ("all", None, [], ["dave@example.org"], False),
    ("", "dave@example.org", [], ["dave@example.org"], True),
    ("", "dave@example.org", [], ["erin@example.org"], False),
    ("", "dave@example.org", ["all"], None, False),
    ("", "dave@example.org", [], [], False),
    ("all", "alice@example.org", ["all"], ["alice@example.org", "bob@example.org"], True),
    ("all", "alice@example.org", ["all"], ["bob@example.org"], False),
    ("all", None, ["all"], ["alice@example.org"], False),
    ("mgmt", "alice@example.org", ["all"], ["alice@example.org"], False),
    ("", None, ["all"], None, False),
    ("", None, [], ["dave@example.org"], False),
    ("", None, [], [], False),
]


def test_visible_truth_table(fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_app", DB)) as conn:
        for caller_aud, caller_email, req, emails, expected in VISIBLE_CASES:
            set_caller(conn, audiences=caller_aud, email=caller_email)
            got = conn.execute("SELECT kh.visible(%s::text[], %s::text[])", (req, emails)).fetchone()[0]
            assert got is expected, (caller_aud, caller_email, req, emails)
        set_caller(conn, audiences="all,hr", email="Alice@example.org", ptype="claude_client", sub="s")
        assert conn.execute("SELECT kh.caller_audiences(), kh.caller_email(), kh.caller_type(),"
                            " kh.caller_sub()").fetchone() == (
            ["all", "hr"], "Alice@example.org", "claude_client", "s")
        set_caller(conn, audiences="", email="", ptype="", sub="")
        assert conn.execute("SELECT kh.caller_audiences(), kh.caller_email(), kh.caller_type(),"
                            " kh.caller_sub()").fetchone() == ([], None, None, None)


def test_no_audience_no_email_caller_sees_nothing_because_open_rows_cannot_exist(fresh_db):
    # kh.visible('{}', NULL) is true for everyone; kh_no_open_rows keeps such rows out.
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        seed_mode(conn)
        f = factories({})
        row = f["entities_cache"]()
        row.update(required_audiences=[], allowed_emails=None)
        with pytest.raises(errors.CheckViolation):
            insert(conn, "entities_cache", row)


def test_claim_seen_and_claim_jti_is_hidden_from_kh_app(fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_app", DB), autocommit=True) as conn:
        exp = NOW + timedelta(minutes=5)
        assert conn.execute("SELECT kh.claim_seen('jti-1', %s)", (exp,)).fetchone()[0] is True
        assert conn.execute("SELECT kh.claim_seen('jti-1', %s)", (exp,)).fetchone()[0] is False
        assert conn.execute("SELECT kh.claim_seen('jti-2', %s)", (exp,)).fetchone()[0] is True
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM kh.claim_jti")


def test_indexer_deletes_only_expired_claim_ids(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        insert(conn, "claim_jti", {"jti": "old", "expires_at": NOW - timedelta(minutes=1)})
        insert(conn, "claim_jti", {"jti": "live", "expires_at": NOW + timedelta(hours=1)})
    with psycopg.connect(fresh_db.dsn("kh_indexer", DB), autocommit=True) as conn:
        assert conn.execute("DELETE FROM kh.claim_jti WHERE jti = 'live'").rowcount == 0
        assert conn.execute("DELETE FROM kh.claim_jti").rowcount == 1
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("INSERT INTO kh.claim_jti VALUES ('x', now())")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("UPDATE kh.claim_jti SET expires_at = now()")
    with psycopg.connect(fresh_db.admin_dsn(DB)) as conn:
        assert [r[0] for r in conn.execute("SELECT jti FROM kh.claim_jti")] == ["live"]


@pytest.fixture
def owned(fresh_db):
    """Rows created by SUB_A that kh_app's caller (audience all) cannot see (audience mgmt)."""
    ids = {"derived": uid(), "candidate": uid(), "conflict": uid(), "raw": uid()}
    hidden = {"required_audiences": ["mgmt"], "allowed_emails": None}
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        seed_mode(conn)
        f = factories({})
        for table, key in (("derived_records", "derived"), ("candidates", "candidate"),
                           ("conflicts", "conflict"), ("raw_records", "raw")):
            row = f[table]() | hidden | {"id": ids[key]}
            if table == "raw_records":
                row |= {"source_system": "meet", "source_ref": "r1"}
            insert(conn, table, row)
    return fresh_db, ids


def test_owns_functions_and_my_raw_id(owned):
    cluster, ids = owned
    with psycopg.connect(cluster.dsn("kh_app", DB), autocommit=True) as conn:
        set_caller(conn, sub=SUB_A)
        assert conn.execute("SELECT count(*) FROM kh.derived_records").fetchone()[0] == 0
        for fn, key in (("owns_derived", "derived"), ("owns_candidate", "candidate"),
                        ("owns_conflict", "conflict")):
            q = sql.SQL("SELECT kh.{}(%s)").format(sql.Identifier(fn))
            assert conn.execute(q, (ids[key],)).fetchone()[0] is True, fn
            assert conn.execute(q, (uid(),)).fetchone()[0] is False, fn
            set_caller(conn, sub=SUB_B)
            assert conn.execute(q, (ids[key],)).fetchone()[0] is False, fn
            set_caller(conn, audiences="", email="", ptype="", sub="")
            assert conn.execute(q, (ids[key],)).fetchone()[0] is False, fn
            set_caller(conn, sub=SUB_A)
        q = "SELECT kh.my_raw_id(%s, %s)"
        assert str(conn.execute(q, ("meet", "r1")).fetchone()[0]) == ids["raw"]
        assert conn.execute(q, ("meet", "r2")).fetchone()[0] is None
        assert conn.execute(q, ("drive", "r1")).fetchone()[0] is None
        set_caller(conn, sub=SUB_A, ptype="claude_client")
        assert conn.execute(q, ("meet", "r1")).fetchone()[0] is None
        set_caller(conn, sub=SUB_B)
        assert conn.execute(q, ("meet", "r1")).fetchone()[0] is None


def _state(cluster, cid):
    with psycopg.connect(cluster.admin_dsn(DB)) as conn:
        return conn.execute("SELECT state, branch, pr_number, pr_url, error FROM kh.candidates"
                            " WHERE id = %s", (cid,)).fetchone()


def test_candidate_mark_transitions(owned):
    cluster, ids = owned
    cid = ids["candidate"]
    mark = "SELECT kh.candidate_mark(%s, %s, %s, %s, %s, %s)"
    with psycopg.connect(cluster.dsn("kh_app", DB), autocommit=True) as conn:
        set_caller(conn, sub=SUB_B)
        assert conn.execute(mark, (cid, "pr_open", "b", 1, "u", None)).fetchone()[0] is False
        assert _state(cluster, cid)[0] == "proposed"
        set_caller(conn, sub=SUB_A)
        for bad in ("promoted", "rejected", "expired", "proposed", "bogus"):
            assert conn.execute(mark, (cid, bad, None, None, None, None)).fetchone()[0] is False, bad
        assert _state(cluster, cid)[0] == "proposed"
        assert conn.execute(mark, (cid, "failed", "bot/promote/x", None, None, "boom")).fetchone()[0]
        assert _state(cluster, cid) == ("failed", "bot/promote/x", None, None, "boom")
        assert conn.execute(mark, (cid, "failed", None, None, None, "again")).fetchone()[0]
        assert _state(cluster, cid) == ("failed", "bot/promote/x", None, None, "again")
        assert conn.execute(mark, (cid, "pr_open", None, 7, "http://pr/7", None)).fetchone()[0]
        assert _state(cluster, cid) == ("pr_open", "bot/promote/x", 7, "http://pr/7", None)
        for bad in ("failed", "pr_open", "promoted", "proposed"):
            assert conn.execute(mark, (cid, bad, None, None, None, None)).fetchone()[0] is False, bad
        assert _state(cluster, cid)[0] == "pr_open"
        assert conn.execute(mark, (uid(), "pr_open", None, None, None, None)).fetchone()[0] is False
    with psycopg.connect(cluster.dsn("kh_indexer", DB), autocommit=True) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(mark, (cid, "failed", None, None, None, None))
