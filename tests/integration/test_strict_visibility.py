"""Migration 0006: kh.strict_visibility and the candidate/conflict visibility triggers (plan 3.8)."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import errors

from tests.integration.test_derived_visibility_sql import (
    admin,  # noqa: F401  (fixture)
    app_conn,
    bare_derived,
    caller,
    commit_error,
    doc,
    insert,
    raw,
)
from tests.support.principals import alice_all, bob_mgmt, carol_hr, wf_token

DB = "kh_fresh"


def uid() -> str:
    return str(uuid.uuid4())


def sv(cluster, who, ids):
    with app_conn(cluster, who) as conn:
        return conn.execute(
            "SELECT n_resolved, required_audiences, allowed_emails FROM kh.strict_visibility(%s::text[])",
            (ids,)).fetchone()


def candidate_row(conn, aud, emails, cid=None, sub="alice") -> str:
    cid = cid or uid()
    insert(conn, "candidates", dict(
        id=cid, state="proposed", domain="product", target_repo="canonical-all", target_path="product/x.md",
        title="t", body_sha="b", dedupe_key=uid(), created_by_sub=sub,
        required_audiences=list(aud), allowed_emails=emails))
    return cid


def candidate_source(conn, cid, kind, sid) -> None:
    insert(conn, "candidate_sources", dict(candidate_id=cid, source_kind=kind, source_id=sid,
                                           src_required_audiences=["all"], src_allowed_emails=None))


def conflict_row(conn, aud, emails, cid=None, sub="alice") -> str:
    cid = cid or uid()
    insert(conn, "conflicts", dict(id=cid, state="open", created_by_sub=sub,
                                   required_audiences=list(aud), allowed_emails=emails))
    return cid


def conflict_item(conn, cid, kind, sid) -> None:
    insert(conn, "conflict_items", dict(conflict_id=cid, item_kind=kind, item_id=sid,
                                        src_required_audiences=["all"], src_allowed_emails=None))


# --- kh.strict_visibility ---------------------------------------------------------------------


def test_union_of_audiences(fresh_db, admin):  # noqa: F811
    ids = [f"doc:{doc(admin, ('mgmt',))}", f"doc:{doc(admin, ('hr',))}"]
    both = caller(("all", "hr", "mgmt"))
    assert sv(fresh_db, both, ids) == (2, ["hr", "mgmt"], None)


def _readable(cluster, who, table):
    with app_conn(cluster, who) as conn:
        return {str(r[0]) for r in conn.execute(f"SELECT id FROM kh.{table}")}


def test_mgmt_and_finance_sources_are_hidden_from_mgmt_only(fresh_db, admin):  # noqa: F811
    d1, d2 = doc(admin, ("mgmt",)), doc(admin, ("finance",))
    mgmt_only, both = caller(("mgmt",)), caller(("finance", "mgmt"))
    ids = [f"doc:{d1}", f"doc:{d2}"]
    assert sv(fresh_db, both, ids) == (2, ["finance", "mgmt"], None)
    assert sv(fresh_db, mgmt_only, ids) == (1, ["mgmt"], None)
    cid, kid = uid(), uid()

    def body():
        candidate_row(admin, ("finance", "mgmt"), None, cid=cid)
        conflict_row(admin, ("finance", "mgmt"), None, cid=kid)
        for sid in (d1, d2):
            candidate_source(admin, cid, "doc", sid)
            conflict_item(admin, kid, "doc", sid)

    assert commit_error(admin, body) is None
    for table, rid in (("candidates", cid), ("conflicts", kid)):
        assert rid not in _readable(fresh_db, mgmt_only, table)
        assert rid in _readable(fresh_db, both, table)


def test_any_of_source_and_hr_source_need_one_of_each(fresh_db, admin):  # noqa: F811
    d1, d2 = doc(admin, ("finance|mgmt",)), doc(admin, ("hr",))
    ids = [f"doc:{d1}", f"doc:{d2}"]
    mgmt_hr, mgmt_only = caller(("hr", "mgmt")), caller(("mgmt",))
    assert sv(fresh_db, mgmt_hr, ids) == (2, ["finance|mgmt", "hr"], None)
    assert sv(fresh_db, mgmt_only, ids) == (1, ["finance|mgmt"], None)
    cid, kid = uid(), uid()

    def body():
        candidate_row(admin, ("finance|mgmt", "hr"), None, cid=cid)
        conflict_row(admin, ("finance|mgmt", "hr"), None, cid=kid)
        for sid in (d1, d2):
            candidate_source(admin, cid, "doc", sid)
            conflict_item(admin, kid, "doc", sid)

    assert commit_error(admin, body) is None
    for table, rid in (("candidates", cid), ("conflicts", kid)):
        assert rid in _readable(fresh_db, mgmt_hr, table)
        assert rid not in _readable(fresh_db, mgmt_only, table)


def test_intersection_of_email_lists(fresh_db, admin):  # noqa: F811
    who = caller(("all",), "dave@example.org")
    a = doc(admin, (), ["a@example.org", "dave@example.org"])
    r = raw(admin, (), ["b@example.org", "dave@example.org"])
    d = bare_derived(admin, ("all",), None)
    assert sv(fresh_db, who, [f"doc:{a}", f"raw:{r}", f"derived:{d}"]) == (3, ["all"], ["dave@example.org"])


def test_hidden_source_lowers_n_resolved(fresh_db, admin):  # noqa: F811
    seen, hidden = doc(admin, ("all",)), doc(admin, ("mgmt",))
    h_raw, h_derived = raw(admin, ("hr",), None), bare_derived(admin, ("mgmt",))
    ids = [f"doc:{seen}", f"doc:{hidden}", f"raw:{h_raw}", f"derived:{h_derived}", f"doc:{uid()}", "doc:nope"]
    assert sv(fresh_db, alice_all, ids) == (1, ["all"], None)
    assert sv(fresh_db, bob_mgmt, ids) == (3, ["all", "mgmt"], None)
    assert sv(fresh_db, carol_hr, ids) == (2, ["all", "hr"], None)


def test_empty_list(fresh_db):
    assert sv(fresh_db, alice_all, []) == (0, [], None)


def test_wf_token_does_not_resolve_raw_it_ingested_but_cannot_see(fresh_db, admin):  # noqa: F811
    r = raw(admin, sub=wf_token.sub, participants=("p@example.org",))
    # Strict: no provenance branch. (kh.derived_visibility resolves it; see 0005.)
    assert sv(fresh_db, wf_token, [f"raw:{r}"]) == (0, [], None)
    with app_conn(fresh_db, wf_token) as conn:
        assert conn.execute("SELECT n_resolved FROM kh.derived_visibility(%s::text[])",
                            ([f"raw:{r}"],)).fetchone()[0] == 1


def test_identical_sources_give_identical_visibility_for_two_callers(fresh_db, admin):  # noqa: F811
    ids = [f"doc:{doc(admin, ('all',))}", f"doc:{doc(admin, ('hr',), None)}",
           f"raw:{raw(admin, ('all',), None)}"]
    a = caller(("all", "hr"), "one@example.org", sub="one")
    b = caller(("all", "hr", "mgmt"), "two@example.org", sub="two")
    assert sv(fresh_db, a, ids) == sv(fresh_db, b, ids) == (3, ["all", "hr"], None)


def test_function_is_invoker_with_app_only_execute(fresh_db, admin):  # noqa: F811
    row = admin.execute("SELECT p.prosecdef, p.proconfig FROM pg_proc p"
                        " WHERE p.oid = 'kh.strict_visibility(text[])'::regprocedure").fetchone()
    assert row == (False, ["search_path=kh, pg_temp"])
    with psycopg.connect(fresh_db.dsn("kh_indexer", DB), autocommit=True) as ix:
        with pytest.raises(errors.InsufficientPrivilege):
            ix.execute("SELECT * FROM kh.strict_visibility('{}'::text[])")


# --- constraint triggers ----------------------------------------------------------------------


@pytest.mark.parametrize(("name", "table"), [("candidate", "candidates"), ("conflict", "conflicts")])
def test_trigger_settings(admin, name, table):  # noqa: F811
    row = admin.execute(
        "SELECT t.tgdeferrable, t.tginitdeferred, t.tgconstraint <> 0, t.tgtype,"
        " pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig"
        " FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid"
        " WHERE t.tgname = %s AND t.tgrelid = ('kh.' || %s)::regclass",
        (f"{name}_visibility_matches", table)).fetchone()
    assert row == (True, True, True, 5, "kh_provenance", True, ["search_path=kh, pg_temp"])


def test_matching_candidate_and_conflict_commit(admin):  # noqa: F811
    d1, d2 = doc(admin, ("hr",), ["a@example.org", "b@example.org"]), doc(admin, ("mgmt",), None)
    r = raw(admin, ("all",), ["b@example.org", "c@example.org"])
    cid, kid = uid(), uid()

    def body():
        candidate_row(admin, ("mgmt", "hr", "all"), ["b@example.org"], cid=cid)
        conflict_row(admin, ("mgmt", "hr", "all"), ["b@example.org"], cid=kid)
        for kind, sid in (("doc", d1), ("doc", d2), ("raw", r)):
            candidate_source(admin, cid, kind, sid)
            conflict_item(admin, kid, kind, sid)

    assert commit_error(admin, body) is None
    assert admin.execute("SELECT (SELECT count(*) FROM kh.candidates),"
                         " (SELECT count(*) FROM kh.conflicts)").fetchone() == (1, 1)


@pytest.mark.parametrize("kind", ["candidate", "conflict"])
@pytest.mark.parametrize(("aud", "emails"), [
    (("hr",), None),
    (("all", "hr"), None),
    (("hr",), ["a@example.org", "z@example.org"]),
    (("hr",), []),
])
def test_mismatching_visibility_fails_at_commit(admin, kind, aud, emails):  # noqa: F811
    d1 = doc(admin, ("hr",), ["a@example.org"])
    d2 = doc(admin, (), ["a@example.org", "b@example.org"])
    rid = uid()

    def body():
        if kind == "candidate":
            candidate_row(admin, aud, emails, cid=rid)
            candidate_source(admin, rid, "doc", d1)
            candidate_source(admin, rid, "doc", d2)
        else:
            conflict_row(admin, aud, emails, cid=rid)
            conflict_item(admin, rid, "doc", d1)
            conflict_item(admin, rid, "doc", d2)

    exc = commit_error(admin, body)
    assert exc is not None and exc.sqlstate == "P0K04"
    assert "does not match" in str(exc)
    assert admin.execute("SELECT (SELECT count(*) FROM kh.candidates),"
                         " (SELECT count(*) FROM kh.conflicts)").fetchone() == (0, 0)


def test_candidate_narrower_than_sources_fails_for_kh_app(fresh_db, admin):  # noqa: F811
    d1 = doc(admin, ("mgmt",))
    cid = uid()
    with app_conn(fresh_db, bob_mgmt) as conn:
        def body():
            candidate_row(conn, ("all",), None, cid=cid, sub=bob_mgmt.sub)
            candidate_source(conn, cid, "doc", d1)

        exc = commit_error(conn, body)
    assert exc is not None and exc.sqlstate == "P0K04"
    assert admin.execute("SELECT count(*) FROM kh.candidates").fetchone()[0] == 0


def test_kh_app_commits_with_function_result(fresh_db, admin):  # noqa: F811
    d1, d2 = doc(admin, ("all",)), doc(admin, ("mgmt",))
    ids = [f"doc:{d1}", f"doc:{d2}"]
    n, aud, emails = sv(fresh_db, bob_mgmt, ids)
    assert n == 2
    cid, kid = uid(), uid()
    with app_conn(fresh_db, bob_mgmt) as conn:
        def body():
            candidate_row(conn, aud, emails, cid=cid, sub=bob_mgmt.sub)
            conflict_row(conn, aud, emails, cid=kid, sub=bob_mgmt.sub)
            for d in (d1, d2):
                candidate_source(conn, cid, "doc", d)
                conflict_item(conn, kid, "doc", d)

        assert commit_error(conn, body) is None
    assert admin.execute("SELECT required_audiences FROM kh.candidates WHERE id = %s",
                         (cid,)).fetchone() == (["all", "mgmt"],)


@pytest.mark.parametrize("kind", ["candidate", "conflict"])
def test_row_without_sources_fails_at_commit(admin, kind):  # noqa: F811
    with pytest.raises(psycopg.Error) as exc:
        with admin.transaction():
            (candidate_row if kind == "candidate" else conflict_row)(admin, ("all",), None)
    assert exc.value.sqlstate == "P0K04"
    assert "no sources" in str(exc.value)


def test_kh_indexer_visibility_update_does_not_fire_triggers(fresh_db, admin):  # noqa: F811
    d1 = doc(admin, ("hr",), None)
    cid, kid = uid(), uid()
    with admin.transaction():
        candidate_row(admin, ("hr",), None, cid=cid)
        candidate_source(admin, cid, "doc", d1)
        conflict_row(admin, ("hr",), None, cid=kid)
        conflict_item(admin, kid, "doc", d1)
    with psycopg.connect(fresh_db.dsn("kh_indexer", DB)) as ix:
        for table in ("candidates", "conflicts"):
            ix.execute(f"UPDATE kh.{table} SET required_audiences = '{{contracts}}',"
                       " allowed_emails = '{c@example.org}'")
        ix.commit()
    for table in ("candidates", "conflicts"):
        assert admin.execute(f"SELECT required_audiences, allowed_emails FROM kh.{table}"
                             ).fetchone() == (["contracts"], ["c@example.org"])
