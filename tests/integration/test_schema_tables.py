"""Baseline schema: tables, columns and constraints (plan 3.5)."""

import psycopg
import pytest

from tests.support.seed import add_chunk, add_doc, add_record

EXPECTED = {
    "usage_events": ["id", "at", "kind", "who", "audiences", "query", "query_norm", "min_tier",
                     "t0_fts_hits", "t3_fts_hits", "owner_keys"],
    "schema_migrations": ["name", "sha256", "applied_at"],
    "claim_jti": ["jti", "expires_at"],
    "groups": ["name", "repo"],
    "labels": ["name", "description", "created_at"],
    "documents": ["id", "repo", "path", "doc_key", "audience", "title", "body", "labels", "review_by",
                  "stale", "commit_sha", "blob_sha", "indexed_at", "updated_at", "supersedes",
                  "superseded_by", "tier"],
    "records": ["id", "audience", "source", "source_ref", "source_url", "title", "blob_key", "excerpt",
                "content_type", "occurred_at", "labels", "written_by", "created_by_sub",
                "created_by_type", "ingested_at", "outdated", "outdated_at", "outdated_by", "outdated_note",
                "replaced_by"],
    "chunks": ["id", "document_id", "record_id", "owner_key", "owner_created_at", "ordinal",
               "heading_path", "text", "content_sha", "embedding", "embedding_model", "tsv", "tier",
               "audience", "labels", "stale"],
    "index_jobs": ["id", "kind", "repo", "payload", "state", "attempts", "last_error", "created_at",
                   "run_after"],
    "repo_state": ["repo", "last_indexed_sha", "indexed_at"],
    "index_alerts": ["id", "repo", "path", "message", "created_at"],
}


@pytest.fixture
def conn(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn("kh_fresh"), autocommit=True) as c:
        c.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all'), ('mgmt', 'kb-mgmt')")
        c.execute("INSERT INTO kh.labels (name) VALUES ('ember')")
        yield c


def test_tables_and_columns(conn):
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'kh' ORDER BY table_name, ordinal_position"
    ).fetchall()
    actual: dict[str, list[str]] = {}
    for t, c in rows:
        actual.setdefault(t, []).append(c)
    assert {k: sorted(v) for k, v in actual.items()} == {k: sorted(v) for k, v in EXPECTED.items()}


def test_embedding_is_nullable_vector_1024(conn):
    row = conn.execute(
        "SELECT is_nullable, format_type(a.atttypid, a.atttypmod) FROM information_schema.columns c "
        "JOIN pg_attribute a ON a.attrelid = 'kh.chunks'::regclass AND a.attname = c.column_name "
        "WHERE c.table_schema = 'kh' AND c.table_name = 'chunks' AND c.column_name = 'embedding'"
    ).fetchone()
    assert row == ("YES", "vector(1024)")


def test_every_table_is_owned_by_the_migrator(conn):
    rows = conn.execute(
        "SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'kh' AND c.relkind = 'r'"
    ).fetchall()
    assert {owner for _, owner in rows} == {"kh_migrator"}
    assert {name for name, _ in rows} == set(EXPECTED)


@pytest.mark.parametrize("bad", ["", "A", "-a", "a b", "a/b", "a" * 64])
def test_group_name_check(conn, bad):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES (%s, 'r')", [bad])


@pytest.mark.parametrize("name", ["product:ember", "a.b-c_d", "x"])
def test_label_name_accepts_legal_names(conn, name):
    conn.execute("INSERT INTO kh.labels (name) VALUES (%s)", [name])


@pytest.mark.parametrize("bad", ["", "A", ":a", "a b"])
def test_label_name_check(conn, bad):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO kh.labels (name) VALUES (%s)", [bad])


def test_group_repo_is_unique(conn):
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('other', 'kb-all')")


def test_documents_audience_must_be_a_group(conn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        add_doc(conn, audience="nope")


def test_documents_unique_keys(conn):
    add_doc(conn, repo="kb-all", path="a.md", doc_key="a")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_doc(conn, repo="kb-all", path="b.md", doc_key="a")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_doc(conn, repo="kb-all", path="a.md", doc_key="b")
    add_doc(conn, repo="kb-mgmt", path="a.md", doc_key="a", audience="mgmt")


def test_records_audience_must_be_a_group(conn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        add_record(conn, audience="nope")


def test_records_unique_key_is_audience_source_ref(conn):
    add_record(conn, audience="all", source="s", source_ref="r")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_record(conn, audience="all", source="s", source_ref="r")
    add_record(conn, audience="mgmt", source="s", source_ref="r")
    add_record(conn, audience="all", source="s2", source_ref="r")


def test_chunk_owner_check(conn):
    d, r = add_doc(conn), add_record(conn)
    add_chunk(conn, document_id=d)
    add_chunk(conn, record_id=r, tier="T3")
    for owners in ({}, {"document_id": d, "record_id": r}):
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            add_chunk(conn, **owners)
        assert exc.value.diag.constraint_name == "chunks_one_owner"


def test_chunk_tier_check(conn):
    add_chunk(conn, document_id=add_doc(conn))
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        conn.execute("UPDATE kh.chunks SET tier = 'T1'")
    assert exc.value.diag.constraint_name == "chunks_tier_check"
    definition = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'chunks_tier_check'"
    ).fetchone()[0]
    assert "'T0a'" in definition


def test_chunks_cascade_on_owner_delete(conn):
    d, r = add_doc(conn), add_record(conn)
    add_chunk(conn, document_id=d)
    add_chunk(conn, record_id=r)
    conn.execute("DELETE FROM kh.documents WHERE id = %s", [d])
    assert conn.execute("SELECT count(*) FROM kh.chunks").fetchone() == (1,)
    conn.execute("DELETE FROM kh.records WHERE id = %s", [r])
    assert conn.execute("SELECT count(*) FROM kh.chunks").fetchone() == (0,)


def test_index_job_kinds(conn):
    conn.execute("INSERT INTO kh.index_jobs (kind) VALUES ('git_push'), ('embed_record')")
    for kind in ("embed_raw", "pr_event"):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO kh.index_jobs (kind) VALUES (%s)", [kind])


def test_tsv_generated(conn):
    d = add_doc(conn)
    add_chunk(conn, document_id=d, heading_path="Intro", text="running quickly")
    row = conn.execute(
        "SELECT tsv::text, tsv @@ to_tsquery('english', 'run & quick') FROM kh.chunks"
    ).fetchone()
    assert row[1] is True
    assert "intro" in row[0]
    with pytest.raises(psycopg.errors.GeneratedAlways):
        conn.execute("UPDATE kh.chunks SET tsv = NULL")
