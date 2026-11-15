import uuid

import psycopg
import pytest

VIS = ["required_audiences", "allowed_emails"]

EXPECTED = {
    "documents": ["id", "repo", "path", "doc_key", "tier", "domain", "title", "body", "frontmatter",
                  "status", "stale", "review_by", "owner", "entities", "supersedes", "superseded_by",
                  "commit_sha", "content_sha", "approved_by", "approved_at", "merge_pr", "pr_url",
                  "indexed_at", "updated_at"] + VIS,
    "document_versions": ["document_id", "commit_sha", "path", "tier", "status", "approved_by",
                          "approved_at", "indexed_at"],
    "chunks": ["id", "document_id", "raw_id", "derived_id", "entities", "ordinal", "heading_path",
               "text", "content_sha", "embedding", "embedding_model", "tsv", "owner_key",
               "owner_created_at", "tier", "domain", "status", "stale", "superseded"] + VIS,
    "entities_cache": ["id", "kind", "name", "owner_team", "status", "attrs", "synced_at"] + VIS,
    "raw_records": ["id", "source_system", "source_ref", "source_url", "series", "folder",
                    "participants", "title", "blob_key", "excerpt", "content_type", "occurred_at",
                    "ingested_by_sub", "ingested_by_type", "ingested_at", "domain", "entities",
                    "generated_by"] + VIS,
    "raw_audience_map": ["kind", "key", "audiences"],
    "derived_records": ["id", "kind", "title", "body", "claims", "domain", "entities", "generated_by",
                        "created_by_sub", "created_by_type", "created_at"] + VIS,
    "derived_sources": ["derived_id", "source_kind", "source_id", "version",
                        "src_required_audiences", "src_allowed_emails"],
    "candidates": ["id", "derived_id", "target_doc_id", "state", "domain", "target_repo", "target_path",
                   "title", "body_sha", "dedupe_key", "branch", "pr_number", "pr_url",
                   "created_by_sub", "created_at", "decided_at", "error"] + VIS,
    "candidate_sources": ["candidate_id", "source_kind", "source_id", "src_required_audiences",
                          "src_allowed_emails"],
    "conflicts": ["id", "state", "domain", "note", "created_by_sub", "created_at", "closed_at",
                  "closed_by_pr"] + VIS,
    "conflict_items": ["conflict_id", "item_kind", "item_id", "src_required_audiences",
                       "src_allowed_emails"],
    "index_jobs": ["id", "kind", "repo", "payload", "state", "attempts", "last_error", "created_at",
                   "run_after"],
    "repo_state": ["repo", "trust_anchor_sha", "last_indexed_sha", "trusted", "untrusted_reason",
                   "config_hash"],
    "index_alerts": ["id", "repo", "path", "message", "created_at"],
}

OPEN = {"required_audiences": [], "allowed_emails": None}


def u():
    return str(uuid.uuid4())


@pytest.fixture
def conn(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn("kh_fresh"), autocommit=True) as c:
        yield c


def insert(conn, table, **cols):
    names = list(cols)
    conn.execute(
        f"INSERT INTO kh.{table} ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))})",
        [cols[n] for n in names],
    )


def ok_vis():
    return {"required_audiences": ["all"], "allowed_emails": None}


def doc(**over):
    row = dict(id=u(), repo="r", path="p" + u(), doc_key="k" + u(), tier="T0", domain="d", title="t",
               body="b", status="current", commit_sha="c", content_sha="s", **ok_vis())
    row.update(over)
    return row


def raw(**over):
    row = dict(id=u(), source_system="s", source_ref=u(), ingested_by_sub="x", ingested_by_type="y",
               **ok_vis())
    row.update(over)
    return row


def derived(**over):
    row = dict(id=u(), kind="summary", title="t", body="b", created_by_sub="x", created_by_type="y",
               **ok_vis())
    row.update(over)
    return row


def candidate(**over):
    row = dict(id=u(), state="proposed", domain="d", target_repo="r", target_path="p", title="t",
               body_sha="b", dedupe_key="k", created_by_sub="x", **ok_vis())
    row.update(over)
    return row


def conflict(**over):
    row = dict(id=u(), state="open", created_by_sub="x", **ok_vis())
    row.update(over)
    return row


def chunk(**over):
    row = dict(id=u(), ordinal=0, text="hello world", content_sha="s", owner_key="doc:x", tier="T0",
               **ok_vis())
    row.update(over)
    return row


def test_tables_and_columns(conn):
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'kh' ORDER BY table_name, ordinal_position"
    ).fetchall()
    actual: dict[str, list[str]] = {}
    for t, c in rows:
        actual.setdefault(t, []).append(c)
    actual.pop("schema_migrations")
    actual.pop("claim_jti")
    assert {k: sorted(v) for k, v in actual.items()} == {k: sorted(v) for k, v in EXPECTED.items()}


def test_embedding_is_nullable_vector_1024(conn):
    row = conn.execute(
        "SELECT is_nullable, format_type(a.atttypid, a.atttypmod) FROM information_schema.columns c "
        "JOIN pg_attribute a ON a.attrelid = 'kh.chunks'::regclass AND a.attname = c.column_name "
        "WHERE c.table_schema = 'kh' AND c.table_name = 'chunks' AND c.column_name = 'embedding'"
    ).fetchone()
    assert row == ("YES", "vector(1024)")


@pytest.mark.parametrize(
    ("table", "make"),
    [
        ("documents", doc),
        ("entities_cache", lambda **o: dict(id=u(), **o)),
        ("raw_records", raw),
        ("derived_records", derived),
        ("candidates", candidate),
        ("conflicts", conflict),
    ],
)
def test_no_open_rows(conn, table, make):
    insert(conn, table, **make(**{**ok_vis(), "allowed_emails": ["a@x.y"], "required_audiences": []}))
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        insert(conn, table, **(make() | OPEN))
    assert exc.value.diag.constraint_name == "kh_no_open_rows"


def test_chunk_open_row_names_constraint(conn):
    d = doc()
    insert(conn, "documents", **d)
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        insert(conn, "chunks", **(chunk(document_id=d["id"]) | OPEN))
    assert exc.value.diag.constraint_name == "kh_no_open_rows"


def test_chunk_owner_check(conn):
    d, r = doc(), raw()
    insert(conn, "documents", **d)
    insert(conn, "raw_records", **r)
    insert(conn, "chunks", **chunk(document_id=d["id"]))
    for owners in ({}, {"document_id": d["id"], "raw_id": r["id"]}):
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            insert(conn, "chunks", **chunk(**owners))
        assert exc.value.diag.constraint_name == "chunks_one_owner"


def test_chunk_cascade_on_document_delete(conn):
    d = doc()
    insert(conn, "documents", **d)
    insert(conn, "chunks", **chunk(document_id=d["id"]))
    conn.execute("DELETE FROM kh.documents WHERE id = %s", [d["id"]])
    assert conn.execute("SELECT count(*) FROM kh.chunks").fetchone() == (0,)


def test_dedupe_key_partial_unique(conn):
    first = candidate()
    insert(conn, "candidates", **first)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(conn, "candidates", **candidate())
    conn.execute("UPDATE kh.candidates SET state = 'rejected' WHERE id = %s", [first["id"]])
    insert(conn, "candidates", **candidate())


def test_tsv_generated(conn):
    d = doc()
    insert(conn, "documents", **d)
    insert(conn, "chunks", **chunk(document_id=d["id"], heading_path="Intro", text="running quickly"))
    row = conn.execute(
        "SELECT tsv::text, tsv @@ to_tsquery('english', 'run & quick') FROM kh.chunks"
    ).fetchone()
    assert row[1] is True
    assert "intro" in row[0]
    with pytest.raises(psycopg.errors.GeneratedAlways):
        conn.execute("UPDATE kh.chunks SET tsv = NULL")
