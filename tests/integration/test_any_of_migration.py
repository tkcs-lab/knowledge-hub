"""Migration 0007: old-form rows are converted to clauses (plan 15 section 3.8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from knowledge_hub.db.migrate import migrate
from knowledge_hub.indexer.rebuild import DRIFT_SQL

DB = "kh_any_of"
WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
SUB = "n8n-token:seed"
VISIBILITY_TABLES = ("documents", "chunks", "raw_records", "derived_records", "candidates",
                     "conflicts", "entities_cache")
SOURCE_TABLES = ("derived_sources", "candidate_sources", "conflict_items")


def u() -> str:
    return str(uuid.uuid4())


def insert(cur, table: str, row: dict) -> None:
    cols = list(row)
    cur.execute(f"INSERT INTO kh.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                list(row.values()))


def seed_old_rows(dsn: str) -> dict[str, str]:
    i = {k: u() for k in ("doc", "raw", "chunk", "derived", "cand", "conf")}
    fm = ["finance", "mgmt"]
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL session_replication_role = replica")
        insert(cur, "raw_audience_map", dict(kind="series", key="s1", audiences=fm))
        insert(cur, "documents", dict(
            id=i["doc"], repo="canonical-all", path="docs/hr.md", doc_key="hr", tier="T0",
            domain="product", title="Doc", body="b", frontmatter=Jsonb({}), status="current",
            stale=False, owner="team", entities=[], supersedes=[], commit_sha="c1", content_sha="s",
            approved_by=["x"], approved_at=WHEN, indexed_at=WHEN, updated_at=WHEN,
            required_audiences=["hr"], allowed_emails=None))
        insert(cur, "raw_records", dict(
            id=i["raw"], source_system="meet", source_ref="r1", series="s1", title="Raw",
            ingested_by_sub=SUB, ingested_by_type="n8n_workflow", ingested_at=WHEN,
            occurred_at=WHEN, domain="technical", entities=[], required_audiences=fm,
            allowed_emails=None))
        insert(cur, "chunks", dict(
            id=i["chunk"], raw_id=i["raw"], entities=[], ordinal=0, heading_path="H", text="t",
            content_sha="cs", owner_key=f"raw:{i['raw']}", owner_created_at=WHEN, tier="T3",
            domain="technical", status="current", stale=False, superseded=False,
            required_audiences=fm, allowed_emails=None))
        insert(cur, "entities_cache", dict(
            id="prd:a", kind="prd", name="A", owner_team="team", status="active", attrs=Jsonb({}),
            synced_at=WHEN, required_audiences=fm, allowed_emails=None))
        insert(cur, "derived_records", dict(
            id=i["derived"], kind="summary", title="D", body="d", domain="technical", entities=[],
            created_by_sub=SUB, created_by_type="n8n_workflow", created_at=WHEN,
            required_audiences=["finance", "hr", "mgmt"], allowed_emails=None))
        insert(cur, "candidates", dict(
            id=i["cand"], derived_id=i["derived"], state="proposed", domain="product",
            target_repo="canonical-all", target_path="p/x.md", title="C", body_sha="b",
            dedupe_key="dk", created_by_sub=SUB, created_at=WHEN,
            required_audiences=["finance", "hr", "mgmt"], allowed_emails=None))
        insert(cur, "conflicts", dict(
            id=i["conf"], state="open", domain="product", note="n", created_by_sub=SUB,
            created_at=WHEN, required_audiences=["finance", "hr", "mgmt"], allowed_emails=None))
        for table, pcol, kcol, icol, pid in (
                ("derived_sources", "derived_id", "source_kind", "source_id", i["derived"]),
                ("candidate_sources", "candidate_id", "source_kind", "source_id", i["cand"]),
                ("conflict_items", "conflict_id", "item_kind", "item_id", i["conf"])):
            for kind, sid, aud in (("raw", i["raw"], fm), ("doc", i["doc"], ["hr"])):
                insert(cur, table, {pcol: pid, kcol: kind, icol: sid,
                                    "src_required_audiences": aud, "src_allowed_emails": None})
        conn.commit()
    return i


def test_old_rows_are_converted_to_clauses(migrated_db):
    with psycopg.connect(migrated_db.admin_dsn(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DB} OWNER kh_migrator")
    with psycopg.connect(migrated_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")
    dsn = migrated_db.dsn("kh_migrator", DB)
    assert migrate(dsn, stop_after="0006_strict_visibility.sql")[-1] == "0006_strict_visibility.sql"
    i = seed_old_rows(migrated_db.admin_dsn(DB))

    assert migrate(dsn, stop_after="0007_any_of_audiences.sql") == ["0007_any_of_audiences.sql"]

    with psycopg.connect(migrated_db.admin_dsn(DB)) as conn:
        def req(table: str, key: str):
            return conn.execute(f"SELECT required_audiences FROM kh.{table} WHERE id = %s",
                                (key,)).fetchone()[0]

        assert req("raw_records", i["raw"]) == ["finance|mgmt"]
        assert req("chunks", i["chunk"]) == ["finance|mgmt"]
        assert req("entities_cache", "prd:a") == ["finance|mgmt"]
        assert req("documents", i["doc"]) == ["hr"]
        for table, key in (("derived_records", "derived"), ("candidates", "cand"),
                           ("conflicts", "conf")):
            assert req(table, i[key]) == ["finance|mgmt", "hr"], table
        conn.commit()  # release the read locks so the migration can alter the tables
        migrate(dsn)  # the remaining migrations (0007_proposals adds chunks.agent_written)
        assert conn.execute(DRIFT_SQL).fetchall() == []
        names = lambda cons: {r[0] for r in conn.execute(  # noqa: E731
            "SELECT conrelid::regclass::text FROM pg_constraint WHERE conname = %s", (cons,))}
        assert names("kh_audience_clauses") == {f"kh.{t}" for t in VISIBILITY_TABLES}
        assert names("kh_src_audience_clauses") == {f"kh.{t}" for t in SOURCE_TABLES}
