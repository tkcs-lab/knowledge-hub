"""Baseline 0001 triggers: label guards, chunk inheritance, embed queue (3.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import errors

from tests.support.seed import add_chunk, add_doc, add_record

DB = "kh_fresh"
SUB = "n8n-token:a"


@pytest.fixture
def admin(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        yield conn


def app_conn(cluster, audiences="all", sub=SUB):
    conn = psycopg.connect(cluster.dsn("kh_app", DB), autocommit=True)
    conn.execute(
        "SELECT set_config('kh.audiences', %s, false),"
        " set_config('kh.principal_type', 'n8n_workflow', false), set_config('kh.sub', %s, false)",
        (audiences, sub),
    )
    return conn


def chunk_row(admin, chunk_id):
    return admin.execute(
        "SELECT owner_key, tier, audience, labels, stale FROM kh.chunks WHERE id = %s", [chunk_id]
    ).fetchone()


# --------------------------------------------------------------------------- labels


def test_unknown_label_on_document_raises_p0k05(admin):
    with pytest.raises(psycopg.Error) as exc:
        add_doc(admin, labels=["ember", "nope"])
    assert exc.value.sqlstate == "P0K05"
    assert "nope" in str(exc.value)
    add_doc(admin, labels=["ember", "other"])


def test_unknown_label_on_record_raises_p0k05(admin):
    with pytest.raises(psycopg.Error) as exc:
        add_record(admin, labels=["nope"])
    assert exc.value.sqlstate == "P0K05"


def test_label_update_is_checked(admin):
    d = add_doc(admin)
    admin.execute("UPDATE kh.documents SET labels = '{ember}' WHERE id = %s", [d])
    with pytest.raises(psycopg.Error) as exc:
        admin.execute("UPDATE kh.documents SET labels = '{nope}' WHERE id = %s", [d])
    assert exc.value.sqlstate == "P0K05"


def test_kh_app_insert_with_unknown_label_raises_p0k05(seeded_db, fresh_db):
    conn = app_conn(fresh_db)
    now = datetime.now(UTC)
    with pytest.raises(psycopg.Error) as exc:
        conn.execute(
            "INSERT INTO kh.records (audience, source, source_ref, title, blob_key, excerpt,"
            " content_type, occurred_at, labels, written_by, created_by_sub, created_by_type)"
            " VALUES ('all', 's', 'r', 't', 'k', 'e', 'text/plain', %s, '{nope}', 'kh-bot', %s,"
            " 'n8n_workflow')",
            (now, SUB),
        )
    assert exc.value.sqlstate == "P0K05"


def test_assert_labels_exist_accepts_empty_and_null(admin):
    admin.execute("SELECT kh.assert_labels_exist('{}'::text[]), kh.assert_labels_exist(NULL)")


def test_deleting_a_used_label_raises_p0k06(admin):
    add_doc(admin, labels=["ember"])
    with pytest.raises(psycopg.Error) as exc:
        admin.execute("DELETE FROM kh.labels WHERE name = 'ember'")
    assert exc.value.sqlstate == "P0K06"


def test_deleting_a_label_used_by_a_record_raises_p0k06(admin):
    add_record(admin, labels=["other"])
    with pytest.raises(psycopg.Error) as exc:
        admin.execute("DELETE FROM kh.labels WHERE name = 'other'")
    assert exc.value.sqlstate == "P0K06"


def test_deleting_an_unused_label_works_and_a_freed_one_too(admin):
    d = add_doc(admin, labels=["ember"])
    admin.execute("DELETE FROM kh.labels WHERE name = 'other'")
    admin.execute("UPDATE kh.documents SET labels = '{}' WHERE id = %s", [d])
    admin.execute("DELETE FROM kh.labels WHERE name = 'ember'")


def test_label_guard_sees_rows_the_caller_cannot(admin):
    """The guard is a definer: an indexer-visible but caller-hidden row still blocks the delete."""
    add_doc(admin, audience="hr", labels=["ember"])
    with pytest.raises(psycopg.Error) as exc:
        admin.execute("DELETE FROM kh.labels WHERE name = 'ember'")
    assert exc.value.sqlstate == "P0K06"


# --------------------------------------------------------------------------- chunks


def test_chunk_of_a_document_copies_its_owner(admin):
    d = add_doc(admin, audience="mgmt", repo="kb-mgmt", labels=["ember"], stale=True)
    c = add_chunk(admin, document_id=d, tier="T3", audience="all", labels=[], stale=False)
    assert chunk_row(admin, c) == (f"doc:{d}", "T0", "mgmt", ["ember"], True)
    created = admin.execute(
        "SELECT c.owner_created_at = d.indexed_at FROM kh.chunks c"
        " JOIN kh.documents d ON d.id = c.document_id WHERE c.id = %s",
        [c],
    ).fetchone()
    assert created == (True,)


def test_chunk_of_a_record_copies_its_owner(admin):
    r = add_record(admin, audience="hr", labels=["other"])
    c = add_chunk(admin, record_id=r, tier="T0", audience="all", stale=True)
    assert chunk_row(admin, c) == (f"rec:{r}", "T3", "hr", ["other"], False)
    assert admin.execute(
        "SELECT c.owner_created_at = r.ingested_at FROM kh.chunks c"
        " JOIN kh.records r ON r.id = c.record_id WHERE c.id = %s",
        [c],
    ).fetchone() == (True,)


def test_indexer_inserts_chunks_through_the_trigger(seeded_db, fresh_db, admin):
    d = add_doc(admin, audience="mgmt", repo="kb-mgmt", labels=["ember"])
    with psycopg.connect(fresh_db.dsn("kh_indexer", DB), autocommit=True) as conn:
        cid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO kh.chunks (id, document_id, owner_key, owner_created_at, ordinal, text, content_sha,"
            " tier, audience) VALUES (%s, %s, '', now(), 0, 't', 's', 'T3', 'all')",
            [cid, d],
        )
    assert chunk_row(admin, cid) == (f"doc:{d}", "T0", "mgmt", ["ember"], False)


def test_document_update_propagates_to_chunks(admin):
    d = add_doc(admin, labels=[])
    c1, c2 = add_chunk(admin, document_id=d), add_chunk(admin, document_id=d, ordinal=1)
    other = add_chunk(admin, document_id=add_doc(admin))
    admin.execute(
        "UPDATE kh.documents SET audience = 'hr', labels = '{ember,other}', stale = true WHERE id = %s", [d]
    )
    for c in (c1, c2):
        assert chunk_row(admin, c)[2:] == ("hr", ["ember", "other"], True)
    assert chunk_row(admin, other)[2:] == ("all", [], False)


@pytest.mark.parametrize(
    ("sql", "want"),
    [
        ("UPDATE kh.documents SET stale = true WHERE id = %s", ("all", [], True)),
        ("UPDATE kh.documents SET labels = '{ember}' WHERE id = %s", ("all", ["ember"], False)),
        ("UPDATE kh.documents SET audience = 'mgmt' WHERE id = %s", ("mgmt", [], False)),
    ],
)
def test_each_propagated_column_reaches_the_chunks(admin, sql, want):
    d = add_doc(admin)
    c = add_chunk(admin, document_id=d)
    admin.execute(sql, [d])
    assert chunk_row(admin, c)[2:] == want


def test_unrelated_document_update_leaves_chunks_alone(admin):
    d = add_doc(admin)
    c = add_chunk(admin, document_id=d)
    admin.execute("UPDATE kh.chunks SET stale = true WHERE id = %s", [c])
    admin.execute("UPDATE kh.documents SET title = 'new' WHERE id = %s", [d])
    assert chunk_row(admin, c)[4] is True  # untouched: the trigger fires only on its columns


# --------------------------------------------------------------------------- embed queue


def jobs(admin):
    return admin.execute("SELECT kind, payload FROM kh.index_jobs ORDER BY id").fetchall()


def test_kh_app_insert_queues_an_embed_record_job(seeded_db, fresh_db, admin):
    admin.execute("DELETE FROM kh.index_jobs")
    rid = str(uuid.uuid4())
    conn = app_conn(fresh_db, "all")
    conn.execute(
        "INSERT INTO kh.records (id, audience, source, source_ref, title, blob_key, excerpt, content_type,"
        " occurred_at, written_by, created_by_sub, created_by_type)"
        " VALUES (%s, 'all', 's', 'r', 't', 'k', 'e', 'text/plain', now(), 'kh-bot', %s, 'n8n_workflow')",
        (rid, SUB),
    )
    assert jobs(admin) == [("embed_record", {"id": rid})]


def test_a_refused_insert_queues_nothing(seeded_db, fresh_db, admin):
    admin.execute("DELETE FROM kh.index_jobs")
    conn = app_conn(fresh_db, "all")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO kh.records (audience, source, source_ref, title, blob_key, excerpt, content_type,"
            " occurred_at, written_by, created_by_sub, created_by_type)"
            " VALUES ('hr', 's', 'r', 't', 'k', 'e', 'text/plain', now(), 'kh-bot', %s, 'n8n_workflow')",
            (SUB,),
        )
    assert jobs(admin) == []


def test_documents_queue_no_job(admin):
    admin.execute("DELETE FROM kh.index_jobs")
    add_doc(admin)
    assert jobs(admin) == []
