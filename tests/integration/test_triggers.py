"""Migration 0004: chunk inheritance, embed queue, raw visibility and source snapshots (3.3, 3.7)."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import errors

DB = "kh_fresh"
SUB = "n8n-token:a"


def uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        yield conn


def app_conn(cluster, audiences="all", email="", ptype="n8n_workflow", sub=SUB):
    conn = psycopg.connect(cluster.dsn("kh_app", DB), autocommit=True)
    conn.execute(
        "SELECT set_config('kh.audiences', %s, false), set_config('kh.email', %s, false),"
        " set_config('kh.principal_type', %s, false), set_config('kh.sub', %s, false)",
        (audiences, email, ptype, sub),
    )
    return conn


def doc(admin, aud=("all",), emails=None, **over) -> str:
    row = dict(id=uid(), repo="canonical-all", path=f"p/{uid()}.md", doc_key=uid(), tier="T0",
               domain="product", title="t", body="b", status="current", commit_sha="c1",
               content_sha="s1", entities=["prd:a"], required_audiences=list(aud),
               allowed_emails=emails)
    row.update(over)
    return insert(admin, "documents", row)


def raw(admin, aud=("all",), emails=None, **over) -> str:
    # Applications cannot choose visibility: set it through the trigger's own inputs.
    row = dict(id=uid(), source_system="meet", source_ref=uid(), ingested_by_sub=SUB,
               ingested_by_type="n8n_workflow", domain="technical", entities=["prd:b"],
               participants=["seed@example.org"], required_audiences=[], allowed_emails=None)
    row.update(over)
    return insert(admin, "raw_records", row)


def derived(admin, **over) -> str:
    row = dict(id=uid(), kind="summary", title="t", body="b", created_by_sub=SUB,
               created_by_type="n8n_workflow", domain="technical", entities=["prd:c"],
               required_audiences=["all"], allowed_emails=None)
    row.update(over)
    # A bare derived row with no sources would fail derived_visibility_matches (0005) at
    # commit; these 0004 tests need one, so insert it with user triggers off.
    with admin.transaction():
        admin.execute("SET LOCAL session_replication_role = replica")
        return insert(admin, "derived_records", row)


def insert(conn, table, row) -> str:
    cols = list(row)
    conn.execute(
        f"INSERT INTO kh.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
        list(row.values()),
    )
    return row.get("id")


def chunk(admin, owner_col: str, owner_id: str, **over) -> str:
    row = dict(id=uid(), ordinal=0, text="hello", content_sha="s", owner_key="wrong",
               tier="T0", domain="wrong", status="disputed", stale=True, superseded=True,
               entities=["x"], required_audiences=["mgmt"], allowed_emails=["z@example.org"],
               **{owner_col: owner_id})
    row.update(over)
    return insert(admin, "chunks", row)


def chunk_row(admin, cid):
    return admin.execute(
        "SELECT owner_key, tier, domain, status, stale, superseded, entities, owner_created_at,"
        " required_audiences, allowed_emails FROM kh.chunks WHERE id = %s", (cid,)).fetchone()


# --- chunks_inherit_owner and owner_propagate_to_chunks ------------------------------------


def test_document_chunk_inherits_owner_values(fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_indexer", DB), autocommit=True) as ix:
        d = doc(ix, aud=("hr",), emails=["a@example.org"], tier="T1", status="superseded",
                stale=True)
        cid = chunk(ix, "document_id", d)
        (key, tier, domain, status, stale, sup, ents, created, req, emails) = chunk_row(ix, cid)
    assert (key, tier, domain, status, stale, sup, ents) == (
        f"doc:{d}", "T1", "product", "superseded", True, True, ["prd:a"])
    assert created is not None
    assert (req, emails) == (["hr"], ["a@example.org"])


def test_raw_chunk_is_t3_and_derived_chunk_is_t2(admin):
    r = raw(admin)
    dr = derived(admin)
    c_raw = chunk(admin, "raw_id", r, tier="T0")
    c_der = chunk(admin, "derived_id", dr, tier="T0")
    row = chunk_row(admin, c_raw)
    assert row[:7] == (f"raw:{r}", "T3", "technical", "current", False, False, ["prd:b"])
    assert row[8:] == ([], ["seed@example.org"])
    row = chunk_row(admin, c_der)
    assert row[:7] == (f"derived:{dr}", "T2", "technical", "current", False, False, ["prd:c"])
    assert row[8:] == (["all"], None)


def test_chunk_update_cannot_change_inherited_values(admin):
    d = doc(admin)
    cid = chunk(admin, "document_id", d)
    admin.execute("UPDATE kh.chunks SET tier = 'T3', required_audiences = '{mgmt}' WHERE id = %s",
                  (cid,))
    row = chunk_row(admin, cid)
    assert row[1] == "T0" and row[8] == ["all"]


def test_document_changes_propagate_to_chunks(admin):
    d = doc(admin)
    cid = chunk(admin, "document_id", d)
    admin.execute(
        "UPDATE kh.documents SET required_audiences = '{mgmt}', allowed_emails = '{b@example.org}',"
        " stale = true, status = 'superseded', domain = 'technical' WHERE id = %s", (d,))
    row = chunk_row(admin, cid)
    assert row[2:6] == ("technical", "superseded", True, True)
    assert row[8:] == (["mgmt"], ["b@example.org"])


def test_raw_visibility_change_propagates_to_chunks(fresh_db, admin):
    r = raw(admin)
    cid = chunk(admin, "raw_id", r)
    with psycopg.connect(fresh_db.dsn("kh_indexer", DB), autocommit=True) as ix:
        ix.execute("UPDATE kh.raw_records SET required_audiences = '{hr}', allowed_emails = NULL"
                   " WHERE id = %s", (r,))
    row = chunk_row(admin, cid)
    assert row[1] == "T3" and row[8:] == (["hr"], None)


def test_derived_visibility_change_propagates_to_chunks(fresh_db, admin):
    dr = derived(admin)
    cid = chunk(admin, "derived_id", dr)
    with psycopg.connect(fresh_db.dsn("kh_indexer", DB), autocommit=True) as ix:
        ix.execute("UPDATE kh.derived_records SET required_audiences = '{contracts}',"
                   " allowed_emails = '{c@example.org}' WHERE id = %s", (dr,))
    row = chunk_row(admin, cid)
    assert row[1] == "T2" and row[8:] == (["contracts"], ["c@example.org"])


# --- embed queue -------------------------------------------------------------------------------


def jobs(admin):
    return admin.execute("SELECT kind, payload FROM kh.index_jobs ORDER BY id").fetchall()


def test_raw_insert_by_kh_app_queues_one_embed_job_it_cannot_read(fresh_db, admin):
    rid = uid()
    with app_conn(fresh_db) as conn:
        insert(conn, "raw_records", dict(
            id=rid, source_system="meet", source_ref="r1", ingested_by_sub=SUB,
            ingested_by_type="n8n_workflow", participants=["P@Example.org"],
            required_audiences=["all"], allowed_emails=None))
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM kh.index_jobs")
    assert jobs(admin) == [("embed_raw", {"id": rid})]


def test_derived_insert_queues_embed_derived_job(fresh_db, admin):
    did, src = uid(), doc(admin)
    with app_conn(fresh_db) as conn, conn.transaction():
        insert(conn, "derived_records", dict(
            id=did, kind="summary", title="t", body="b", created_by_sub=SUB,
            created_by_type="n8n_workflow", required_audiences=["all"], allowed_emails=None))
        insert(conn, "derived_sources", dict(
            derived_id=did, source_kind="doc", source_id=src,
            src_required_audiences=["all"], src_allowed_emails=None))
    assert jobs(admin) == [("embed_derived", {"id": did})]


# --- raw_records_assign_visibility -----------------------------------------------------------


def vis(admin, rid):
    return admin.execute("SELECT required_audiences, allowed_emails FROM kh.raw_records"
                         " WHERE id = %s", (rid,)).fetchone()


def ingest(conn, **over) -> str:
    row = dict(id=uid(), source_system="meet", source_ref=uid(), ingested_by_sub=SUB,
               ingested_by_type="n8n_workflow", required_audiences=["hr"],
               allowed_emails=["evil@example.org"])
    row.update(over)
    return insert(conn, "raw_records", row)


def test_mapped_series_gives_mapped_audiences_and_null_emails(fresh_db, admin):
    admin.execute("INSERT INTO kh.raw_audience_map VALUES ('series', 's1', '{mgmt,all}')")
    with app_conn(fresh_db, audiences="all,mgmt,hr") as conn:
        rid = ingest(conn, series="s1", participants=["a@example.org"])
    assert vis(admin, rid) == (["all", "mgmt"], None)


def test_mapped_series_and_folder_give_the_union(fresh_db, admin):
    admin.execute("INSERT INTO kh.raw_audience_map VALUES ('series', 's1', '{all}'),"
                  " ('folder', 'f1', '{hr}')")
    with app_conn(fresh_db, audiences="all,hr") as conn:
        rid = ingest(conn, series="s1", folder="f1")
    assert vis(admin, rid) == (["all", "hr"], None)


def test_unmapped_gives_empty_audiences_and_lowercased_participants(fresh_db, admin):
    with app_conn(fresh_db, audiences="") as conn:
        rid = ingest(conn, series="nope", participants=["Alice@Example.ORG", "bob@example.org"])
    assert vis(admin, rid) == ([], ["alice@example.org", "bob@example.org"])


@pytest.mark.parametrize("participants", [None, []])
def test_unmapped_without_participants_raises_p0k01(fresh_db, admin, participants):
    with app_conn(fresh_db) as conn, pytest.raises(psycopg.Error) as exc:
        ingest(conn, participants=participants)
    assert exc.value.sqlstate == "P0K01"
    assert admin.execute("SELECT count(*) FROM kh.raw_records").fetchone() == (0,)


def test_mapped_audiences_not_held_by_caller_raise_p0k02(fresh_db, admin):
    admin.execute("INSERT INTO kh.raw_audience_map VALUES ('series', 's1', '{all,mgmt}')")
    with app_conn(fresh_db, audiences="all") as conn, pytest.raises(psycopg.Error) as exc:
        ingest(conn, series="s1", participants=["a@example.org"])
    assert exc.value.sqlstate == "P0K02"
    assert admin.execute("SELECT count(*) FROM kh.raw_records").fetchone() == (0,)


def test_supplied_visibility_is_overwritten(fresh_db, admin):
    admin.execute("INSERT INTO kh.raw_audience_map VALUES ('folder', 'f1', '{all}')")
    with app_conn(fresh_db, audiences="all") as conn:
        mapped = ingest(conn, folder="f1", required_audiences=["contracts"],
                        allowed_emails=["x@example.org"])
        email_only = ingest(conn, participants=["Q@example.org"], required_audiences=["all"],
                            allowed_emails=None)
    assert vis(admin, mapped) == (["all"], None)
    assert vis(admin, email_only) == ([], ["q@example.org"])


# --- sources_snapshot_visibility -------------------------------------------------------------


def seed_sources(admin):
    d = doc(admin, aud=("hr",), emails=["a@example.org"])
    r = raw(admin)
    admin.execute("UPDATE kh.raw_records SET required_audiences = '{mgmt}', allowed_emails = NULL"
                  " WHERE id = %s", (r,))
    dr = derived(admin, required_audiences=["contracts"])
    parents = (derived(admin), uid(), uid())
    insert(admin, "candidates", dict(
        id=parents[1], state="proposed", domain="product", target_repo="canonical-all",
        target_path="product/x.md", title="t", body_sha="s", dedupe_key=uid(), created_by_sub=SUB,
        required_audiences=["all"], allowed_emails=None))
    insert(admin, "conflicts", dict(id=parents[2], state="open", created_by_sub=SUB,
                                    required_audiences=["all"], allowed_emails=None))
    return {"doc": (d, ["hr"], ["a@example.org"]), "raw": (r, ["mgmt"], None),
            "derived": (dr, ["contracts"], None)}, parents


def test_source_snapshot_copies_live_visibility_and_overwrites_supplied(admin):
    sources, (derived_id, cand_id, conf_id) = seed_sources(admin)
    wrong = {"src_required_audiences": ["all"], "src_allowed_emails": ["w@example.org"]}
    for kind, (sid, req, emails) in sources.items():
        insert(admin, "derived_sources", dict(derived_id=derived_id, source_kind=kind,
                                              source_id=sid, **wrong))
        insert(admin, "candidate_sources", dict(candidate_id=cand_id, source_kind=kind,
                                                source_id=sid, **wrong))
        insert(admin, "conflict_items", dict(conflict_id=conf_id, item_kind=kind, item_id=sid,
                                             **wrong))
        for table, kcol, icol in (("derived_sources", "source_kind", "source_id"),
                                  ("candidate_sources", "source_kind", "source_id"),
                                  ("conflict_items", "item_kind", "item_id")):
            got = admin.execute(
                f"SELECT src_required_audiences, src_allowed_emails FROM kh.{table}"
                f" WHERE {kcol} = %s AND {icol} = %s", (kind, sid)).fetchone()
            assert got == (req, emails), (table, kind)


def test_source_snapshot_works_for_kh_app_citing_rows_it_cannot_see(fresh_db, admin):
    sources, (derived_id, *_rest) = seed_sources(admin)
    with app_conn(fresh_db, audiences="all") as conn:
        insert(conn, "derived_sources", dict(
            derived_id=derived_id, source_kind="doc", source_id=sources["doc"][0],
            src_required_audiences=["all"], src_allowed_emails=None))
    assert admin.execute("SELECT src_required_audiences, src_allowed_emails FROM kh.derived_sources"
                         ).fetchone() == (["hr"], ["a@example.org"])


@pytest.mark.parametrize(
    ("table", "kcol", "icol", "parent"),
    [("derived_sources", "source_kind", "source_id", "derived_id"),
     ("candidate_sources", "source_kind", "source_id", "candidate_id"),
     ("conflict_items", "item_kind", "item_id", "conflict_id")],
)
@pytest.mark.parametrize("kind", ["doc", "raw", "derived"])
def test_missing_source_raises_p0k03(admin, table, kcol, icol, parent, kind):
    _sources, (derived_id, cand_id, conf_id) = seed_sources(admin)
    pid = {"derived_id": derived_id, "candidate_id": cand_id, "conflict_id": conf_id}[parent]
    with pytest.raises(psycopg.Error) as exc:
        insert(admin, table, {parent: pid, kcol: kind, icol: uid(),
                              "src_required_audiences": ["all"], "src_allowed_emails": None})
    assert exc.value.sqlstate == "P0K03"


def test_malformed_source_id_raises_p0k03(admin):
    _sources, (derived_id, *_rest) = seed_sources(admin)
    with pytest.raises(psycopg.Error) as exc:
        insert(admin, "derived_sources", dict(derived_id=derived_id, source_kind="doc",
                                              source_id="not-a-uuid", src_required_audiences=["all"],
                                              src_allowed_emails=None))
    assert exc.value.sqlstate == "P0K03"


# --- ownership and definer settings -----------------------------------------------------------


def test_trigger_functions_owners_and_search_paths(admin):
    rows = dict(admin.execute(
        "SELECT p.proname, pg_get_userbyid(p.proowner) || '|' || p.prosecdef || '|'"
        " || coalesce(p.proconfig::text, '') FROM pg_proc p JOIN pg_namespace n"
        " ON n.oid = p.pronamespace WHERE n.nspname = 'kh' AND p.prorettype = 'trigger'::regtype"
    ).fetchall())
    path = '{"search_path=kh, pg_temp"}'
    assert rows["raw_records_enqueue_embed"] == f"kh_migrator|true|{path}"
    assert rows["derived_records_enqueue_embed"] == f"kh_migrator|true|{path}"
    assert rows["raw_records_assign_visibility"] == f"kh_provenance|true|{path}"
    assert rows["sources_snapshot_visibility"] == f"kh_provenance|true|{path}"
    assert rows["chunks_inherit_owner"].startswith("kh_migrator|false|")
