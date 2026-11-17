"""Migration 0009_proposals: kh.proposals, proposal_mark, count_proposals_today (plan 16 3.8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors

DB = "kh_fresh"
ALICE = "sub-alice"
BOB = "sub-bob"


def uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        yield conn


def conn_as(cluster, role="kh_app", sub=ALICE, audiences="all", email="a@example.org"):
    conn = psycopg.connect(cluster.dsn(role, DB), autocommit=True)
    conn.execute(
        "SELECT set_config('kh.audiences', %s, false), set_config('kh.email', %s, false),"
        " set_config('kh.principal_type', 'claude_client', false), set_config('kh.sub', %s, false)",
        (audiences, email, sub),
    )
    return conn


def row(**over) -> dict:
    kind = over.get("kind", "doc")
    base = dict(
        id=uid(), kind=kind, state="pending", repo="canonical-all", path="product/x.md",
        doc_key=uid(), domain="product", title="t", content_sha="s",
        dedupe_key=None if kind == "note" else uid(), requester_login="alice",
        created_by_sub=ALICE, required_audiences=["all"], allowed_emails=None,
    )
    base.update(over)
    return base


def insert(conn, **over) -> str:
    r = row(**over)
    cols = list(r)
    conn.execute(
        f"INSERT INTO kh.proposals ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
        list(r.values()),
    )
    return r["id"]


def mark(conn, pid, state, branch=None, commit=None, pr=None, url=None, error=None) -> bool:
    return conn.execute(
        "SELECT kh.proposal_mark(%s, %s, %s, %s, %s, %s, %s)", (pid, state, branch, commit, pr, url, error)
    ).fetchone()[0]


def state_of(admin, pid):
    return admin.execute(
        "SELECT state, branch, commit_sha, pr_number, pr_url, error FROM kh.proposals WHERE id = %s", (pid,)
    ).fetchone()


# --------------------------------------------------------------------------- RLS


def test_kh_app_inserts_its_own_row_and_reads_only_its_own(fresh_db, admin):
    with conn_as(fresh_db, sub=ALICE) as alice:
        mine = insert(alice, id=uid())
    insert(admin, created_by_sub=BOB)  # same audience, another caller
    with conn_as(fresh_db, sub=ALICE) as alice:
        assert alice.execute("SELECT id::text FROM kh.proposals").fetchall() == [(mine,)]
    with conn_as(fresh_db, sub=BOB) as bob:
        assert bob.execute("SELECT count(*) FROM kh.proposals").fetchone() == (1,)
        assert bob.execute("SELECT count(*) FROM kh.proposals WHERE id = %s", (mine,)).fetchone() == (0,)


def test_own_row_with_audiences_the_caller_lost_is_hidden(fresh_db, admin):
    insert(admin, created_by_sub=ALICE, required_audiences=["mgmt"])
    with conn_as(fresh_db, sub=ALICE, audiences="all") as alice:
        assert alice.execute("SELECT count(*) FROM kh.proposals").fetchone() == (0,)
    with conn_as(fresh_db, sub=ALICE, audiences="all,mgmt") as alice:
        assert alice.execute("SELECT count(*) FROM kh.proposals").fetchone() == (1,)


def test_kh_app_cannot_insert_for_another_sub(fresh_db):
    with conn_as(fresh_db, sub=ALICE) as alice, pytest.raises(errors.InsufficientPrivilege):
        insert(alice, created_by_sub=BOB)


@pytest.mark.parametrize("over", [
    {"state": "pr_open"}, {"state": "committed"}, {"state": "failed"},
    {"pr_number": 3}, {"required_audiences": ["mgmt"]},
])
def test_kh_app_insert_must_be_pending_without_pr_number_and_visible(fresh_db, over):
    with conn_as(fresh_db) as alice, pytest.raises(errors.InsufficientPrivilege):
        insert(alice, **over)


def test_kh_app_cannot_update_or_delete_directly(fresh_db):
    with conn_as(fresh_db) as alice:
        pid = insert(alice)
        with pytest.raises(errors.InsufficientPrivilege):
            alice.execute("UPDATE kh.proposals SET state = 'merged' WHERE id = %s", (pid,))
        with pytest.raises(errors.InsufficientPrivilege):
            alice.execute("DELETE FROM kh.proposals WHERE id = %s", (pid,))


def test_indexer_updates_state_but_not_other_columns(fresh_db, admin):
    pid = insert(admin, state="pr_open", pr_number=7)
    with conn_as(fresh_db, role="kh_indexer") as ix:
        assert ix.execute("SELECT count(*) FROM kh.proposals").fetchone() == (1,)
        ix.execute("UPDATE kh.proposals SET state = 'merged', decided_at = now(), pr_url = 'u',"
                   " required_audiences = '{mgmt}' WHERE id = %s", (pid,))
        with pytest.raises(errors.InsufficientPrivilege):
            ix.execute("UPDATE kh.proposals SET title = 'x' WHERE id = %s", (pid,))
        with pytest.raises(errors.InsufficientPrivilege):
            ix.execute("DELETE FROM kh.proposals WHERE id = %s", (pid,))
    assert admin.execute("SELECT state, pr_url, required_audiences FROM kh.proposals").fetchone() == (
        "merged", "u", ["mgmt"])


# --------------------------------------------------------------------------- constraints


def test_note_has_no_dedupe_key_and_the_others_have_one(admin):
    insert(admin, kind="note")
    for kind in ("doc", "edit"):
        insert(admin, kind=kind)
    with pytest.raises(errors.CheckViolation):
        insert(admin, kind="note", dedupe_key="k")
    for kind in ("doc", "edit"):
        with pytest.raises(errors.CheckViolation):
            insert(admin, kind=kind, dedupe_key=None)


def test_kind_state_and_open_row_checks(admin):
    with pytest.raises(errors.CheckViolation):
        insert(admin, kind="other", dedupe_key=None)
    with pytest.raises(errors.CheckViolation):
        insert(admin, state="open")
    with pytest.raises(errors.CheckViolation) as exc:
        insert(admin, required_audiences=[])
    assert exc.value.diag.constraint_name == "kh_no_open_rows"


def test_dedupe_index_allows_a_second_row_after_a_final_state(admin):
    key = "dk"
    first = insert(admin, dedupe_key=key)
    with pytest.raises(errors.UniqueViolation):
        insert(admin, dedupe_key=key)
    for state in ("merged", "closed", "failed"):
        admin.execute("UPDATE kh.proposals SET state = %s WHERE id = %s", (state, first))
        second = insert(admin, dedupe_key=key)
        admin.execute("DELETE FROM kh.proposals WHERE id = %s", (second,))
    admin.execute("UPDATE kh.proposals SET state = 'pr_open' WHERE id = %s", (first,))
    with pytest.raises(errors.UniqueViolation):
        insert(admin, dedupe_key=key)
    for _ in range(2):  # notes carry no key: any number of them
        insert(admin, kind="note")


# --------------------------------------------------------------------------- proposal_mark


def test_note_transitions(fresh_db, admin):
    with conn_as(fresh_db) as alice:
        ok, bad_state, failed = (insert(alice, kind="note") for _ in range(3))
        assert mark(alice, bad_state, "pr_open", pr=1) is False
        assert state_of(admin, bad_state)[0] == "pending"
        assert mark(alice, ok, "committed", branch="main", commit="c1") is True
        assert state_of(admin, ok) == ("committed", "main", "c1", None, None, None)
        assert mark(alice, ok, "failed") is False  # committed is final for the caller
        assert mark(alice, failed, "failed", error="boom") is True
        assert state_of(admin, failed)[5] == "boom"
        assert mark(alice, failed, "committed", commit="c2", error=None) is True  # failed -> committed
        assert state_of(admin, failed) == ("committed", None, "c2", None, None, None)


@pytest.mark.parametrize("kind", ["doc", "edit"])
def test_doc_and_edit_transitions(fresh_db, admin, kind):
    with conn_as(fresh_db) as alice:
        ok, bad, failed = (insert(alice, kind=kind) for _ in range(3))
        assert mark(alice, bad, "committed", commit="c") is False
        assert state_of(admin, bad)[0] == "pending"
        assert mark(alice, ok, "pr_open", branch="b", commit="c", pr=4, url="https://x/4") is True
        assert state_of(admin, ok) == ("pr_open", "b", "c", 4, "https://x/4", None)
        assert mark(alice, ok, "failed") is False  # pr_open is final for the caller
        assert mark(alice, failed, "failed", branch="b2", error="e") is True
        assert mark(alice, failed, "pr_open", pr=9, url="u", error=None) is True
        # null arguments keep the stored value, the error is replaced
        assert state_of(admin, failed) == ("pr_open", "b2", None, 9, "u", None)
        for state in ("pending", "merged", "closed", "nope"):
            other = insert(alice, kind=kind)
            assert mark(alice, other, state) is False


def test_mark_refuses_another_sub_and_changes_nothing(fresh_db, admin):
    with conn_as(fresh_db, sub=ALICE) as alice:
        pid = insert(alice)
    with conn_as(fresh_db, sub=BOB) as bob:
        assert mark(bob, pid, "pr_open", pr=1) is False
        assert mark(bob, uid(), "pr_open", pr=1) is False
    assert state_of(admin, pid) == ("pending", None, None, None, None, None)


def test_marking_one_row_leaves_the_callers_other_rows_and_other_callers_rows(fresh_db, admin):
    with conn_as(fresh_db, sub=ALICE) as alice:
        target, sibling = insert(alice), insert(alice)
        theirs = insert(admin, created_by_sub=BOB)
        assert mark(alice, target, "pr_open", branch="b", pr=2, url="u") is True
    assert state_of(admin, sibling) == ("pending", None, None, None, None, None)
    assert state_of(admin, theirs) == ("pending", None, None, None, None, None)


def test_mark_is_definer_only_for_kh_app(fresh_db):
    with conn_as(fresh_db, role="kh_indexer") as ix, pytest.raises(errors.InsufficientPrivilege):
        mark(ix, uid(), "failed")
    with conn_as(fresh_db, role="kh_indexer") as ix, pytest.raises(errors.InsufficientPrivilege):
        ix.execute("SELECT kh.count_proposals_today('{note}', null, null, null, now())")


# --------------------------------------------------------------------------- count


def count(conn, kinds, domain=None, repo=None, login=None, since=None) -> int:
    since = since or datetime.now(UTC) - timedelta(days=1)
    return conn.execute(
        "SELECT kh.count_proposals_today(%s, %s, %s, %s, %s)", (kinds, domain, repo, login, since)
    ).fetchone()[0]


def test_count_includes_rows_of_other_callers_and_hidden_rows_but_returns_a_number(fresh_db, admin):
    insert(admin, kind="doc", created_by_sub=BOB, required_audiences=["hr"])
    insert(admin, kind="edit", created_by_sub=ALICE)
    insert(admin, kind="note", created_by_sub=BOB, required_audiences=["finance"])
    with conn_as(fresh_db, sub=ALICE, audiences="all") as alice:
        assert alice.execute("SELECT count(*) FROM kh.proposals").fetchone() == (1,)  # RLS
        assert count(alice, ["doc", "edit"]) == 2
        assert count(alice, ["note"]) == 1
        assert count(alice, ["doc", "edit", "note"]) == 3
        assert count(alice, []) == 0


def test_count_honours_each_filter(fresh_db, admin):
    insert(admin, kind="doc", domain="product", repo="canonical-all", requester_login="alice")
    insert(admin, kind="doc", domain="product", repo="canonical-hr", requester_login="bob")
    insert(admin, kind="edit", domain="legal", repo="canonical-all", requester_login="alice")
    insert(admin, kind="note", domain="product", repo="notes-all", requester_login="alice")
    with conn_as(fresh_db) as c:
        both = ["doc", "edit"]
        assert count(c, both) == 3
        assert count(c, both, domain="product") == 2
        assert count(c, both, repo="canonical-all") == 2
        assert count(c, both, login="alice") == 2
        assert count(c, both, domain="product", repo="canonical-all") == 1
        assert count(c, both, domain="product", repo="canonical-all", login="bob") == 0
        assert count(c, ["note"], login="alice") == 1
        assert count(c, ["note"], login="bob") == 0


def test_count_since_is_inclusive_and_counts_failed_rows(fresh_db, admin):
    now = datetime.now(UTC)
    insert(admin, state="failed", created_at=now - timedelta(hours=2))
    insert(admin, created_at=now - timedelta(hours=30))
    with conn_as(fresh_db) as c:
        assert count(c, ["doc"], since=now - timedelta(days=1)) == 1
        assert count(c, ["doc"], since=now - timedelta(hours=30, seconds=1)) == 2
        assert count(c, ["doc"], since=now) == 0


# --------------------------------------------------------------------------- agent_written


def test_agent_written_columns_default_false(admin):
    for table in ("documents", "chunks"):
        row_ = admin.execute(
            "SELECT is_nullable, column_default, data_type FROM information_schema.columns"
            " WHERE table_schema = 'kh' AND table_name = %s AND column_name = 'agent_written'", (table,)
        ).fetchone()
        assert row_ == ("NO", "false", "boolean")


def test_inherit_owner_function_keeps_its_hardening(admin):
    cfg, secdef = admin.execute(
        "SELECT p.proconfig, p.prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'kh' AND p.proname = 'chunks_inherit_owner'").fetchone()
    assert cfg == ["search_path=kh, pg_temp"] and secdef is False
    granted = admin.execute(
        "SELECT has_function_privilege('kh_app', 'kh.chunks_inherit_owner()', 'EXECUTE')").fetchone()
    assert granted == (False,)
