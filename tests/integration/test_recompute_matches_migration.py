"""``kh recompute-visibility`` reaches the rows migration 0007 produces (plan 15 section 3.8)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from knowledge_hub.config import Config
from knowledge_hub.db.migrate import migrate
from knowledge_hub.jobs.recompute import recompute_in

WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
SUB = "n8n-token:seed"
AUDS = ["all", "mgmt", "hr", "contracts", "finance"]
M = Config(audiences=AUDS, raw_audience_map={"series:s1": ["finance", "mgmt"]})
IDS = {
    "doc": "00000000-0000-0000-0000-0000000000d1",
    "raw": "00000000-0000-0000-0000-0000000000a1",
    "chunk": "00000000-0000-0000-0000-0000000000c1",
    "derived": "00000000-0000-0000-0000-0000000000e1",
    "cand": "00000000-0000-0000-0000-0000000000f1",
    "conf": "00000000-0000-0000-0000-0000000000b1",
}
TABLES = ("raw_records", "derived_records", "candidates", "conflicts", "chunks")
SOURCES = (
    ("derived_sources", "derived_id", "source_kind", "source_id"),
    ("candidate_sources", "candidate_id", "source_kind", "source_id"),
    ("conflict_items", "conflict_id", "item_kind", "item_id"),
)


def insert(cur, table: str, row: dict) -> None:
    cols = list(row)
    cur.execute(f"INSERT INTO kh.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                list(row.values()))


def make_db(cluster, name: str) -> str:
    with psycopg.connect(cluster.admin_dsn(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {name} OWNER kh_migrator")
    with psycopg.connect(cluster.admin_dsn(name), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")
    return cluster.dsn("kh_migrator", name)


def seed(admin_dsn: str, *, raw: list[str], mapped: list[str], hr: list[str]) -> None:
    """Rows of the old, singleton form (written with triggers off), the same ids in every database."""
    i = IDS
    with psycopg.connect(admin_dsn) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL session_replication_role = replica")
        insert(cur, "raw_audience_map", dict(kind="series", key="s1", audiences=mapped))
        insert(cur, "documents", dict(
            id=i["doc"], repo="canonical-hr", path="docs/hr.md", doc_key="hr", tier="T0",
            domain="product", title="Doc", body="b", frontmatter=Jsonb({}), status="current",
            stale=False, owner="team", entities=[], supersedes=[], commit_sha="c1", content_sha="s",
            approved_by=["x"], approved_at=WHEN, indexed_at=WHEN, updated_at=WHEN,
            required_audiences=hr, allowed_emails=None))
        insert(cur, "raw_records", dict(
            id=i["raw"], source_system="meet", source_ref="r1", series="s1", title="Raw",
            ingested_by_sub=SUB, ingested_by_type="n8n_workflow", ingested_at=WHEN,
            occurred_at=WHEN, domain="technical", entities=[], required_audiences=raw,
            allowed_emails=None))
        insert(cur, "chunks", dict(
            id=i["chunk"], raw_id=i["raw"], entities=[], ordinal=0, heading_path="H", text="t",
            content_sha="cs", owner_key=f"raw:{i['raw']}", owner_created_at=WHEN, tier="T3",
            domain="technical", status="current", stale=False, superseded=False,
            required_audiences=raw, allowed_emails=None))
        both = sorted({*raw, *hr})
        insert(cur, "derived_records", dict(
            id=i["derived"], kind="summary", title="D", body="d", domain="technical", entities=[],
            created_by_sub=SUB, created_by_type="n8n_workflow", created_at=WHEN,
            required_audiences=both, allowed_emails=None))
        insert(cur, "candidates", dict(
            id=i["cand"], derived_id=i["derived"], state="proposed", domain="product",
            target_repo="canonical-all", target_path="p/x.md", title="C", body_sha="b",
            dedupe_key="dk", created_by_sub=SUB, created_at=WHEN,
            required_audiences=both, allowed_emails=None))
        insert(cur, "conflicts", dict(
            id=i["conf"], state="open", domain="product", note="n", created_by_sub=SUB,
            created_at=WHEN, required_audiences=both, allowed_emails=None))
        for table, pcol, kcol, icol in SOURCES:
            pid = i[{"derived_sources": "derived", "candidate_sources": "cand",
                     "conflict_items": "conf"}[table]]
            for kind, sid, aud in (("raw", i["raw"], raw), ("doc", i["doc"], hr)):
                insert(cur, table, {pcol: pid, kcol: kind, icol: sid,
                                    "src_required_audiences": aud, "src_allowed_emails": None})
        conn.commit()


def visibility(admin_dsn: str) -> dict[str, list]:
    out: dict[str, list] = {}
    with psycopg.connect(admin_dsn) as conn:
        for t in TABLES:
            out[t] = conn.execute(
                f"SELECT id, required_audiences, allowed_emails FROM kh.{t} ORDER BY id").fetchall()
        for table, pcol, kcol, icol in SOURCES:
            out[table] = conn.execute(
                f"SELECT {pcol}, {kcol}, {icol}, src_required_audiences, src_allowed_emails"
                f" FROM kh.{table} ORDER BY 1, 2, 3").fetchall()
    return out


def xmins(admin_dsn: str) -> dict[str, list]:
    with psycopg.connect(admin_dsn) as conn:
        return {t: conn.execute(f"SELECT id, xmin::text FROM kh.{t} ORDER BY id").fetchall()
                for t in TABLES} | {
            table: conn.execute(f"SELECT {pcol}, {kcol}, {icol}, xmin::text FROM kh.{table}"
                                " ORDER BY 1, 2, 3").fetchall()
            for table, pcol, kcol, icol in SOURCES}


async def recompute(indexer_dsn: str, config: Config) -> None:
    async with await psycopg.AsyncConnection.connect(indexer_dsn) as conn:
        async with conn.transaction():
            await recompute_in(conn, config)


async def test_recompute_gives_what_the_migration_gives(migrated_db):
    cluster = migrated_db
    # A: old-form rows under M, then migration 0007.
    dsn_a = make_db(cluster, "kh_rm_a")
    assert migrate(dsn_a, stop_after="0006_strict_visibility.sql")[-1] == "0006_strict_visibility.sql"
    seed(cluster.admin_dsn("kh_rm_a"), raw=["finance", "mgmt"], mapped=["finance", "mgmt"], hr=["hr"])
    migrate(dsn_a)
    # B: the same ids under the old map [mgmt], then recompute with M.
    dsn_b = make_db(cluster, "kh_rm_b")
    migrate(dsn_b)
    seed(cluster.admin_dsn("kh_rm_b"), raw=["mgmt"], mapped=["mgmt"], hr=["hr"])
    await recompute(cluster.dsn("kh_indexer", "kh_rm_b"), M)

    a, b = visibility(cluster.admin_dsn("kh_rm_a")), visibility(cluster.admin_dsn("kh_rm_b"))
    assert a["raw_records"] == [(UUID(IDS["raw"]), ["finance|mgmt"], None)]
    assert a["derived_records"] == [(UUID(IDS["derived"]), ["finance|mgmt", "hr"], None)]
    assert a == b

    # A second recompute on A rewrites no row.
    before = xmins(cluster.admin_dsn("kh_rm_a"))
    await recompute(cluster.dsn("kh_indexer", "kh_rm_a"), M)
    assert xmins(cluster.admin_dsn("kh_rm_a")) == before
    assert visibility(cluster.admin_dsn("kh_rm_a")) == a
