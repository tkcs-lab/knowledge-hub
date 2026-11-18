"""Migration 0004 (tier T0a): the document tier column, the chunk check and the two triggers."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from tests.support.seed import add_chunk, add_doc, add_usage

pytestmark = pytest.mark.integration

DB = "kh_fresh"


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all')")
        yield conn


def _chunk_tier(admin, chunk_id) -> str:
    return admin.execute("SELECT tier FROM kh.chunks WHERE id = %s", (chunk_id,)).fetchone()[0]


def test_document_tier_defaults_to_t0(admin):
    doc = add_doc(admin)
    assert admin.execute("SELECT tier FROM kh.documents WHERE id = %s", (doc,)).fetchone()[0] == "T0"


def test_document_tier_rejects_unknown_value(admin):
    for bad in ("T3", "T1"):
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            add_doc(admin, tier=bad)
        assert exc.value.diag.constraint_name == "documents_tier_check"


def test_chunk_of_a_t0a_document_is_t0a(admin):
    doc = add_doc(admin, tier="T0a")
    chunk = add_chunk(admin, document_id=doc)  # the helper passes tier T0; the trigger overwrites it
    assert _chunk_tier(admin, chunk) == "T0a"
    plain = add_chunk(admin, document_id=add_doc(admin))
    assert _chunk_tier(admin, plain) == "T0"


def test_changing_document_tier_updates_its_chunks(admin):
    doc = add_doc(admin)
    chunk = add_chunk(admin, document_id=doc)
    admin.execute("UPDATE kh.documents SET tier = 'T0a' WHERE id = %s", (doc,))
    assert _chunk_tier(admin, chunk) == "T0a"
    admin.execute("UPDATE kh.documents SET tier = 'T0' WHERE id = %s", (doc,))
    assert _chunk_tier(admin, chunk) == "T0"


def test_usage_event_accepts_min_tier_t0a(admin):
    add_usage(admin, id=str(uuid.uuid4()), min_tier="T0a")
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        add_usage(admin, id=str(uuid.uuid4()), min_tier="T1")
    assert exc.value.diag.constraint_name == "usage_events_min_tier_check"
