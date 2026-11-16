"""Migration 0005: kh.derived_visibility and derived_visibility_matches (plan 3.7, 3.3)."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import errors

from knowledge_hub.auth.claims import Caller
from tests.support.principals import alice_all, bob_mgmt, carol_hr, dave_none, wf_token

DB = "kh_fresh"


def uid() -> str:
    return str(uuid.uuid4())


def caller(audiences: tuple[str, ...], email: str | None = None, sub: str = "x",
           ptype: str = "claude_client") -> Caller:
    return Caller(sub=sub, principal_type=ptype, email=email, audiences=frozenset(audiences), token_id=None)


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        yield conn


def app_conn(cluster, who: Caller) -> psycopg.Connection:
    conn = psycopg.connect(cluster.dsn("kh_app", DB), autocommit=True)
    conn.execute(
        "SELECT set_config('kh.audiences', %s, false), set_config('kh.email', %s, false),"
        " set_config('kh.principal_type', %s, false), set_config('kh.sub', %s, false)",
        (",".join(sorted(who.audiences)), who.email or "", who.principal_type, who.sub),
    )
    return conn


def insert(conn, table, row) -> str | None:
    cols = list(row)
    conn.execute(
        f"INSERT INTO kh.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
        list(row.values()),
    )
    return row.get("id")


def doc(admin, aud=("all",), emails=None, sha="c1") -> str:
    return insert(admin, "documents", dict(
        id=uid(), repo="canonical-all", path=f"p/{uid()}.md", doc_key=uid(), tier="T0",
        domain="product", title="t", body="b", status="current", commit_sha=sha, content_sha="s",
        required_audiences=list(aud), allowed_emails=emails))


def raw(admin, aud=None, emails=None, sub="n8n-token:other", participants=("seed@example.org",)) -> str:
    """A raw record; with ``aud`` given, its trigger-assigned visibility is then overwritten."""
    rid = insert(admin, "raw_records", dict(
        id=uid(), source_system="meet", source_ref=uid(), ingested_by_sub=sub,
        ingested_by_type="n8n_workflow", participants=list(participants),
        required_audiences=[], allowed_emails=None))
    if aud is not None:
        admin.execute("UPDATE kh.raw_records SET required_audiences = %s, allowed_emails = %s"
                      " WHERE id = %s", (list(aud), emails, rid))
    return rid


def derived_row(conn, aud, emails, sub="n8n-token:other", did=None) -> str:
    return insert(conn, "derived_records", dict(
        id=did or uid(), kind="summary", title="t", body="b", created_by_sub=sub,
        created_by_type="n8n_workflow", required_audiences=list(aud), allowed_emails=emails))


def bare_derived(admin, aud=("all",), emails=None, sub="n8n-token:other") -> str:
    """A derived record to cite as a source, inserted with user triggers off."""
    with admin.transaction():
        admin.execute("SET LOCAL session_replication_role = replica")
        return derived_row(admin, aud, emails, sub)


def source(conn, derived_id, kind, sid) -> None:
    # Snapshot columns are overwritten by sources_snapshot_visibility (0004).
    insert(conn, "derived_sources", dict(derived_id=derived_id, source_kind=kind, source_id=sid,
                                         src_required_audiences=["all"], src_allowed_emails=None))


def dv(cluster, who: Caller, ids: list[str]):
    with app_conn(cluster, who) as conn:
        return conn.execute(
            "SELECT n_resolved, required_audiences, allowed_emails, versions"
            " FROM kh.derived_visibility(%s::text[])", (ids,)).fetchone()


# --- kh.derived_visibility --------------------------------------------------------------------


def test_union_of_mgmt_and_hr_needs_a_caller_holding_both(fresh_db, admin):
    ids = [f"doc:{doc(admin, ('mgmt',))}", f"doc:{doc(admin, ('hr',))}"]
    both = caller(("all", "hr", "mgmt"))
    assert dv(fresh_db, both, ids) == (2, ["hr", "mgmt"], None, ["c1", "c1"])
    assert dv(fresh_db, bob_mgmt, ids) == (1, ["mgmt"], None, ["c1", None])
    assert dv(fresh_db, carol_hr, ids) == (1, ["hr"], None, [None, "c1"])


def readable_ids(cluster, who: Caller, table: str) -> set[str]:
    with app_conn(cluster, who) as conn:
        return {str(r[0]) for r in conn.execute(f"SELECT id FROM kh.{table}")}


def test_mgmt_and_finance_sources_are_hidden_from_mgmt_only(fresh_db, admin):
    ids = [f"doc:{doc(admin, ('mgmt',))}", f"doc:{doc(admin, ('finance',))}"]
    mgmt_only, both = caller(("mgmt",)), caller(("finance", "mgmt"))
    assert dv(fresh_db, both, ids)[:3] == (2, ["finance", "mgmt"], None)
    assert dv(fresh_db, mgmt_only, ids)[:3] == (1, ["mgmt"], None)
    did = uid()

    def body():
        derived_row(admin, ("finance", "mgmt"), None, did=did)
        for sid in ids:
            source(admin, did, "doc", sid.split(":")[1])

    assert commit_error(admin, body) is None
    assert did not in readable_ids(fresh_db, mgmt_only, "derived_records")
    assert did in readable_ids(fresh_db, both, "derived_records")


def test_any_of_source_and_hr_source_need_one_of_each(fresh_db, admin):
    ids = [f"doc:{doc(admin, ('finance|mgmt',))}", f"doc:{doc(admin, ('hr',))}"]
    mgmt_hr, mgmt_only = caller(("hr", "mgmt")), caller(("mgmt",))
    assert dv(fresh_db, mgmt_hr, ids)[:3] == (2, ["finance|mgmt", "hr"], None)
    assert dv(fresh_db, mgmt_only, ids)[:3] == (1, ["finance|mgmt"], None)
    did = uid()

    def body():
        derived_row(admin, ("finance|mgmt", "hr"), None, did=did)
        for sid in ids:
            source(admin, did, "doc", sid.split(":")[1])

    assert commit_error(admin, body) is None
    assert did in readable_ids(fresh_db, mgmt_hr, "derived_records")
    assert did not in readable_ids(fresh_db, mgmt_only, "derived_records")
    assert did not in readable_ids(fresh_db, caller(("finance", "mgmt")), "derived_records")


def test_intersection_of_email_lists(fresh_db, admin):
    who = caller(("all",), "dave@example.org")
    a = doc(admin, (), ["a@example.org", "dave@example.org"])
    r = raw(admin, (), ["b@example.org", "dave@example.org"])
    d = bare_derived(admin, ("all",), None)
    got = dv(fresh_db, who, [f"doc:{a}", f"raw:{r}", f"derived:{d}"])
    assert got == (3, ["all"], ["dave@example.org"], ["c1", None, None])


def test_disjoint_email_lists_give_empty_not_null(fresh_db, admin):
    # Through kh.visible every email list contains the caller's email, so an empty
    # intersection needs the provenance branch: two raw records of the token, no shared attendee.
    r1 = raw(admin, sub=wf_token.sub, participants=("a@example.org",))
    r2 = raw(admin, sub=wf_token.sub, participants=("b@example.org",))
    assert dv(fresh_db, wf_token, [f"raw:{r1}", f"raw:{r2}"]) == (2, [], [], [None, None])


def test_sources_without_email_lists_give_null(fresh_db, admin):
    a, b = doc(admin, ("all",)), raw(admin, ("all",), None)
    assert dv(fresh_db, alice_all, [f"doc:{a}", f"raw:{b}"]) == (2, ["all"], None, ["c1", None])


def test_unseen_source_lowers_n_resolved(fresh_db, admin):
    seen, hidden = doc(admin, ("all",)), doc(admin, ("mgmt",))
    raw_hidden = raw(admin, ("hr",), None)
    got = dv(fresh_db, alice_all, [f"doc:{seen}", f"doc:{hidden}", f"raw:{raw_hidden}"])
    assert got == (1, ["all"], None, ["c1", None, None])


@pytest.mark.parametrize("kind", ["doc", "raw", "derived"])
def test_missing_and_hidden_uuid_give_identical_results(fresh_db, admin, kind):
    hidden = {"doc": lambda: doc(admin, ("mgmt",)),
              "raw": lambda: raw(admin, ("mgmt",), None),
              "derived": lambda: bare_derived(admin, ("mgmt",))}[kind]()
    got_hidden = dv(fresh_db, alice_all, [f"{kind}:{hidden}"])
    got_missing = dv(fresh_db, alice_all, [f"{kind}:{uid()}"])
    assert got_hidden == got_missing == (0, [], None, [None])


@pytest.mark.parametrize("bad", ["doc:not-a-uuid", "nope:" + "0" * 36, "", "doc:", "DOC:x"])
def test_malformed_ids_do_not_resolve_and_do_not_error(fresh_db, admin, bad):
    assert dv(fresh_db, alice_all, [bad]) == (0, [], None, [None])


def test_empty_list(fresh_db):
    assert dv(fresh_db, alice_all, []) == (0, [], None, [])


def test_wf_token_resolves_attendee_only_raw_it_ingested(fresh_db, admin):
    r = raw(admin, sub=wf_token.sub, participants=("P@Example.org", "q@example.org"))
    assert admin.execute("SELECT required_audiences, allowed_emails FROM kh.raw_records WHERE id = %s",
                         (r,)).fetchone() == ([], ["p@example.org", "q@example.org"])
    # wf_token has no email, so kh.visible is false: only the provenance branch resolves it.
    with app_conn(fresh_db, wf_token) as conn:
        assert conn.execute("SELECT count(*) FROM kh.raw_records").fetchone()[0] == 0
    assert dv(fresh_db, wf_token, [f"raw:{r}"]) == (1, [], ["p@example.org", "q@example.org"], [None])


def test_wf_token_does_not_resolve_raw_another_token_ingested(fresh_db, admin):
    r = raw(admin, sub="n8n-token:t2", participants=("p@example.org",))
    assert dv(fresh_db, wf_token, [f"raw:{r}"]) == (0, [], None, [None])


def test_provenance_branch_needs_the_n8n_workflow_type(fresh_db, admin):
    r = raw(admin, sub=wf_token.sub, participants=("p@example.org",))
    same_sub_person = caller(("all",), None, sub=wf_token.sub, ptype="claude_client")
    assert dv(fresh_db, same_sub_person, [f"raw:{r}"])[0] == 0


def test_document_versions_only_for_resolved_documents(fresh_db, admin):
    seen, hidden = doc(admin, ("all",), sha="aaa"), doc(admin, ("mgmt",), sha="bbb")
    r = raw(admin, ("all",), None)
    d = bare_derived(admin, ("all",))
    got = dv(fresh_db, alice_all, [f"doc:{seen}", f"doc:{hidden}", f"raw:{r}", f"derived:{d}"])
    assert got == (3, ["all"], None, ["aaa", None, None, None])


def test_provenance_branch_does_not_apply_to_documents_or_derived_rows(fresh_db, admin):
    # Rows the token "wrote" but cannot see: an email-only derived row created by the token,
    # and a hidden document. Neither resolves.
    d = bare_derived(admin, (), ["p@example.org"], sub=wf_token.sub)
    hidden_doc = doc(admin, (), ["p@example.org"])
    assert dv(fresh_db, wf_token, [f"derived:{d}"]) == (0, [], None, [None])
    assert dv(fresh_db, wf_token, [f"doc:{hidden_doc}"]) == (0, [], None, [None])


def test_function_settings_and_grants(fresh_db, admin):
    row = admin.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig FROM pg_proc p"
        " WHERE p.oid = 'kh.derived_visibility(text[])'::regprocedure").fetchone()
    assert row == (True, "kh_provenance", ["search_path=kh, pg_temp"])
    grants = {r for (r,) in admin.execute(
        "SELECT grantee::regrole::text FROM pg_proc p, aclexplode(p.proacl)"
        " WHERE p.oid = 'kh.derived_visibility(text[])'::regprocedure AND privilege_type = 'EXECUTE'")}
    assert grants == {"kh_app", "kh_provenance"}  # the owner keeps its implicit grant
    with psycopg.connect(fresh_db.dsn("kh_indexer", DB), autocommit=True) as ix:
        with pytest.raises(errors.InsufficientPrivilege):
            ix.execute("SELECT * FROM kh.derived_visibility('{}'::text[])")


# --- derived_visibility_matches -------------------------------------------------------------------


def test_trigger_settings(admin):
    row = admin.execute(
        "SELECT t.tgdeferrable, t.tginitdeferred, t.tgconstraint <> 0, t.tgtype,"
        " pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig"
        " FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid"
        " WHERE t.tgname = 'derived_visibility_matches' AND t.tgrelid = 'kh.derived_records'::regclass"
    ).fetchone()
    # tgtype 1 | 4: ROW, INSERT only (AFTER).
    assert row == (True, True, True, 5, "kh_provenance", True, ["search_path=kh, pg_temp"])


def commit_error(conn, body) -> psycopg.Error | None:
    try:
        with conn.transaction():
            body()
    except psycopg.Error as exc:
        return exc
    return None


def test_matching_visibility_commits(admin):
    d1, d2 = doc(admin, ("hr",), ["a@example.org", "b@example.org"]), doc(admin, ("mgmt",), None)
    r = raw(admin, ("all",), ["b@example.org", "c@example.org"])
    did = uid()

    def body():
        derived_row(admin, ("mgmt", "hr", "all"), ["b@example.org"], did=did)
        source(admin, did, "doc", d1)
        source(admin, did, "doc", d2)
        source(admin, did, "raw", r)

    assert commit_error(admin, body) is None
    assert admin.execute("SELECT count(*) FROM kh.derived_records WHERE id = %s", (did,)).fetchone()[0] == 1


@pytest.mark.parametrize(("aud", "emails"), [
    (("hr",), None),                              # narrower audiences, no email list
    (("all", "hr"), None),                        # wider audiences
    (("hr",), ["a@example.org", "z@example.org"]),  # a different email list
    (("hr",), []),                                # empty instead of the intersection
])
def test_mismatching_visibility_fails_at_commit(admin, aud, emails):
    d1 = doc(admin, ("hr",), ["a@example.org"])
    d2 = doc(admin, (), ["a@example.org", "b@example.org"])
    did = uid()

    def body():
        derived_row(admin, aud, emails, did=did)  # the insert itself succeeds (deferred check)
        source(admin, did, "doc", d1)
        source(admin, did, "doc", d2)

    exc = commit_error(admin, body)
    assert exc is not None and exc.sqlstate == "P0K04"
    assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone()[0] == 0


def test_null_versus_empty_emails_is_a_mismatch(admin):
    d1 = doc(admin, ("hr",), None)
    did = uid()

    def body():
        derived_row(admin, ("hr",), [], did=did)
        source(admin, did, "doc", d1)

    assert commit_error(admin, body).sqlstate == "P0K04"


def test_derived_row_without_sources_fails_at_commit(admin):
    did = uid()
    with pytest.raises(psycopg.Error) as exc:
        with admin.transaction():
            derived_row(admin, ("all",), None, did=did)
            # Visible inside the transaction: the check is deferred to commit.
            assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone()[0] == 1
    assert exc.value.sqlstate == "P0K04"
    assert "no sources" in str(exc.value)
    assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone()[0] == 0


def test_trigger_does_not_fire_on_update_by_kh_indexer(fresh_db, admin):
    d1 = doc(admin, ("hr",), None)
    did = uid()
    with admin.transaction():
        derived_row(admin, ("hr",), None, did=did)
        source(admin, did, "doc", d1)
    with psycopg.connect(fresh_db.dsn("kh_indexer", DB)) as ix:
        ix.execute("UPDATE kh.derived_records SET required_audiences = '{contracts}',"
                   " allowed_emails = '{c@example.org}' WHERE id = %s", (did,))
        ix.commit()
    assert admin.execute("SELECT required_audiences, allowed_emails FROM kh.derived_records"
                         " WHERE id = %s", (did,)).fetchone() == (["contracts"], ["c@example.org"])


def test_kh_app_write_path_with_function_result_commits(fresh_db, admin):
    """wf_token cites a raw record only it can resolve, plus a visible document."""
    r = raw(admin, sub=wf_token.sub, participants=("p@example.org",))
    d1 = doc(admin, ("all",), None, sha="abc")
    ids = [f"raw:{r}", f"doc:{d1}"]
    n, aud, emails, versions = dv(fresh_db, wf_token, ids)
    assert (n, aud, emails, versions) == (2, ["all"], ["p@example.org"], [None, "abc"])
    did = uid()
    with app_conn(fresh_db, wf_token) as conn:
        def body():
            derived_row(conn, aud, emails, sub=wf_token.sub, did=did)
            insert(conn, "derived_sources", dict(
                derived_id=did, source_kind="raw", source_id=r, version=versions[0],
                src_required_audiences=["all"], src_allowed_emails=None))
            insert(conn, "derived_sources", dict(
                derived_id=did, source_kind="doc", source_id=d1, version=versions[1],
                src_required_audiences=["all"], src_allowed_emails=None))

        assert commit_error(conn, body) is None
    assert admin.execute("SELECT required_audiences, allowed_emails FROM kh.derived_records"
                         " WHERE id = %s", (did,)).fetchone() == (["all"], ["p@example.org"])


def test_kh_app_cannot_store_wider_visibility_than_its_sources(fresh_db, admin):
    d1 = doc(admin, (), ["dave@example.org"])
    did = uid()
    with app_conn(fresh_db, dave_none) as conn:
        def body():
            derived_row(conn, ("all",), None, sub=dave_none.sub, did=did)
            source(conn, did, "doc", d1)

        exc = commit_error(conn, body)
    assert exc is not None and exc.sqlstate == "P0K04"
    assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone()[0] == 0
