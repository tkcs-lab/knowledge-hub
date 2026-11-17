"""Deterministic rows for the leak tests plus small insert helpers (ids come back as text)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import psycopg

from tests.support.principals import KH_CONFIG, alice_all, bob_mgmt, carol_hr

WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

# group -> the caller who writes that group's seed rows
SEED_WRITERS = {"all": alice_all, "mgmt": bob_mgmt, "hr": carol_hr}
SEED_LABELS = {"ember": "Ember product", "other": "Another label"}


def _insert(conn: psycopg.Connection, table: str, row: dict) -> str:
    cols = list(row)
    got = conn.execute(
        f"INSERT INTO kh.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
        list(row.values()),
    ).fetchone()
    return str(got[0])


def add_doc(conn: psycopg.Connection, **fields) -> str:
    """Insert a document (defaults fill what ``fields`` leaves out) and return its id."""
    n = uuid.uuid4().hex
    row = dict(
        repo="kb-all", path=f"{n}.md", doc_key=n, audience="all", title="Doc", body="Body",
        review_by=date(2027, 1, 1), commit_sha="c1", blob_sha=f"b-{n}",
    )
    row.update(fields)
    return _insert(conn, "documents", row)


def add_record(conn: psycopg.Connection, **fields) -> str:
    """Insert a record (defaults fill what ``fields`` leaves out) and return its id."""
    n = uuid.uuid4().hex
    row = dict(
        audience="all", source="meet", source_ref=n, title="Rec", blob_key=f"records/{n}",
        excerpt="excerpt", content_type="text/plain", occurred_at=WHEN, written_by="kh-bot",
        created_by_sub=alice_all.sub, created_by_type=alice_all.principal_type,
    )
    row.update(fields)
    return _insert(conn, "records", row)


def add_chunk(conn: psycopg.Connection, **fields) -> str:
    """Insert a chunk of ``document_id`` or ``record_id``; the owner row supplies the rest."""
    n = uuid.uuid4().hex
    row = dict(
        owner_key="", owner_created_at=WHEN, ordinal=0, text="hello world", content_sha=f"s-{n}",
        tier="T0", audience="all",
    )
    row.update(fields)
    return _insert(conn, "chunks", row)


def seed_rows(admin_dsn: str) -> dict[str, dict[str, str]]:
    """Seed the groups of ``KH_CONFIG``, labels and one document, record and chunk per group.

    Returns ``{table: {group: id}}``. Runs as the sandbox superuser, so RLS does not apply; the
    triggers do (a seed record queues an ``embed_record`` job, chunks copy their owner).
    """
    ids: dict[str, dict[str, str]] = {"documents": {}, "records": {}, "chunks": {}, "record_chunks": {}}
    with psycopg.connect(admin_dsn) as conn:
        for name in KH_CONFIG.groups:
            conn.execute("INSERT INTO kh.groups (name, repo) VALUES (%s, %s)",
                         (name, KH_CONFIG.repo_for_group(name)))
        for name, desc in SEED_LABELS.items():
            conn.execute("INSERT INTO kh.labels (name, description) VALUES (%s, %s)", (name, desc))
        for group, caller in SEED_WRITERS.items():
            repo = KH_CONFIG.repo_for_group(group)
            doc = add_doc(conn, repo=repo, doc_key=f"doc-{group}", path=f"{group}.md", audience=group,
                          title=f"Doc {group}", body=f"Body {group}", blob_sha=f"blob-{group}")
            rec = add_record(conn, audience=group, source="seed", source_ref=f"ref-{group}",
                             title=f"Rec {group}", created_by_sub=caller.sub,
                             created_by_type=caller.principal_type)
            ids["documents"][group] = doc
            ids["records"][group] = rec
            ids["chunks"][group] = add_chunk(conn, document_id=doc, text=f"chunk of doc {group}")
            ids["record_chunks"][group] = add_chunk(conn, record_id=rec, tier="T3",
                                                    text=f"chunk of record {group}")
        conn.commit()
    return ids
