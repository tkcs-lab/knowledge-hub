"""Deterministic rows for the leak tests: one row per visibility table and visibility case."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

# case name -> (required_audiences, allowed_emails)
CASES: dict[str, tuple[list[str], list[str] | None]] = {
    "all": (["all"], None),
    "mgmt": (["mgmt"], None),
    "hr": (["hr"], None),
    "all_mgmt": (["all", "mgmt"], None),
    "attendee": ([], ["dave@example.org"]),
    "empty_emails": ([], []),
}

VISIBILITY_TABLES = (
    "documents",
    "chunks",
    "raw_records",
    "derived_records",
    "candidates",
    "conflicts",
    "entities_cache",
)

WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

# Chunks whose inherited columns differ from the owner row (plan 3.3, "kh check" drift query).
DRIFT_SQL = """
SELECT c.id FROM kh.chunks c JOIN kh.documents d ON d.id = c.document_id
 WHERE (c.owner_key, c.tier, c.domain, c.status, c.stale, c.superseded, c.entities,
        c.owner_created_at, c.required_audiences, c.allowed_emails)
       IS DISTINCT FROM ('doc:' || d.id, d.tier, d.domain, d.status, d.stale, d.status = 'superseded',
                         d.entities, d.indexed_at, d.required_audiences, d.allowed_emails)
UNION ALL
SELECT c.id FROM kh.chunks c JOIN kh.raw_records r ON r.id = c.raw_id
 WHERE (c.owner_key, c.tier, c.domain, c.status, c.stale, c.superseded, c.entities,
        c.owner_created_at, c.required_audiences, c.allowed_emails)
       IS DISTINCT FROM ('raw:' || r.id, 'T3'::text, r.domain, 'current'::text, false, false,
                         r.entities, r.ingested_at, r.required_audiences, r.allowed_emails)
UNION ALL
SELECT c.id FROM kh.chunks c JOIN kh.derived_records r ON r.id = c.derived_id
 WHERE (c.owner_key, c.tier, c.domain, c.status, c.stale, c.superseded, c.entities,
        c.owner_created_at, c.required_audiences, c.allowed_emails)
       IS DISTINCT FROM ('derived:' || r.id, 'T2'::text, r.domain, 'current'::text, false, false,
                         r.entities, r.created_at, r.required_audiences, r.allowed_emails)
"""

SEED_SUB = "n8n-token:seed"


def _id(table: str, case: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kh-seed/{table}/{case}"))


def _insert(cur: psycopg.Cursor, table: str, row: dict) -> None:
    cols = list(row)
    cur.execute(
        f"INSERT INTO kh.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
        list(row.values()),
    )


def seed_rows(admin_dsn: str) -> dict[str, dict[str, str]]:
    """Insert the seed and return ``{table: {case: id}}`` (ids as text).

    Runs on the sandbox-superuser connection with ``session_replication_role = replica`` so no
    user trigger fires; every column is therefore written explicitly and consistently.
    """
    ids: dict[str, dict[str, str]] = {}
    with psycopg.connect(admin_dsn) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL session_replication_role = replica")
        for case, (aud, emails) in CASES.items():
            vis = {"required_audiences": aud, "allowed_emails": emails}
            doc, raw, der = _id("documents", case), _id("raw_records", case), _id("derived_records", case)
            cand, conf = _id("candidates", case), _id("conflicts", case)
            for table, key in (
                ("documents", doc),
                ("raw_records", raw),
                ("derived_records", der),
                ("candidates", cand),
                ("conflicts", conf),
                ("chunks", _id("chunks", case)),
                ("entities_cache", f"prd:{case}"),
            ):
                ids.setdefault(table, {})[case] = key

            _insert(cur, "documents", dict(
                id=doc, repo="canonical-all", path=f"docs/{case}.md", doc_key=f"doc-{case}", tier="T0",
                domain="product", title=f"Doc {case}", body=f"Body {case}", frontmatter=Jsonb({}),
                status="current", stale=False, review_by=None, owner="team", entities=["prd:a"],
                supersedes=[], superseded_by=None, commit_sha="c1", content_sha=f"s-{case}",
                approved_by=["x"], stamp="reviewed", approved_at=WHEN, merge_pr=1, pr_url=None,
                indexed_at=WHEN,
                updated_at=WHEN, **vis))
            _insert(cur, "document_versions", dict(
                document_id=doc, commit_sha="c1", path=f"docs/{case}.md", tier="T0", status="current",
                approved_by=["x"], stamp="reviewed", approved_at=WHEN, indexed_at=WHEN))
            _insert(cur, "raw_records", dict(
                id=raw, source_system="meet", source_ref=f"ref-{case}", source_url=None, series=None,
                folder=None, participants=None, title=f"Raw {case}", blob_key=None, excerpt="e",
                content_type="text/plain", occurred_at=WHEN, ingested_by_sub=SEED_SUB,
                ingested_by_type="n8n_workflow", ingested_at=WHEN, domain="technical",
                entities=["prd:b"], generated_by=None, **vis))
            _insert(cur, "derived_records", dict(
                id=der, kind="summary", title=f"Derived {case}", body="d", claims=None,
                domain="technical", entities=["prd:c"], generated_by=None, created_by_sub=SEED_SUB,
                created_by_type="n8n_workflow", created_at=WHEN, **vis))
            _insert(cur, "entities_cache", dict(
                id=f"prd:{case}", kind="prd", name=f"Entity {case}", owner_team="team", status="active",
                attrs=Jsonb({}), synced_at=WHEN, **vis))
            _insert(cur, "candidates", dict(
                id=cand, derived_id=der, target_doc_id=doc, state="proposed", domain="product",
                target_repo="canonical-all", target_path=f"p/{case}.md", title=f"Cand {case}",
                body_sha="b", dedupe_key=f"dedupe-{case}", branch=None, pr_number=None, pr_url=None,
                created_by_sub=SEED_SUB, created_at=WHEN, decided_at=None, error=None, **vis))
            _insert(cur, "conflicts", dict(
                id=conf, state="open", domain="product", note="n", created_by_sub=SEED_SUB,
                created_at=WHEN, closed_at=None, closed_by_pr=None, **vis))
            snap = {"src_required_audiences": aud, "src_allowed_emails": emails}
            _insert(cur, "derived_sources", dict(
                derived_id=der, source_kind="doc", source_id=doc, version=None, **snap))
            _insert(cur, "candidate_sources", dict(
                candidate_id=cand, source_kind="doc", source_id=doc, **snap))
            _insert(cur, "conflict_items", dict(
                conflict_id=conf, item_kind="doc", item_id=doc, **snap))
            # One chunk per case, owned by the document; inherited columns copied from it.
            _insert(cur, "chunks", dict(
                id=ids["chunks"][case], document_id=doc, raw_id=None, derived_id=None, entities=["prd:a"],
                ordinal=0, heading_path="H", text=f"chunk {case}", content_sha=f"cs-{case}",
                embedding=None, embedding_model=None, owner_key=f"doc:{doc}", owner_created_at=WHEN,
                tier="T0", domain="product", status="current", stale=False, superseded=False, **vis))
        cur.execute(DRIFT_SQL)
        drift = cur.fetchall()
        assert drift == [], f"seed chunks drift from their owners: {drift}"
        conn.commit()
    return ids
