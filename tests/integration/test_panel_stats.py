"""Panel stats and read API against a real database (numbers asserted one by one)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import psycopg
import pytest

from knowledge_hub.config import GroupConfig, KhConfig, TechRepoConfig
from knowledge_hub.indexer.worker import MAX_ATTEMPTS
from knowledge_hub.panel import stats
from knowledge_hub.panel.app import build_panel_app
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.settings import PanelSettings
from tests.support.pg import FRESH_DB
from tests.support.seed import add_chunked_doc, add_chunked_record, add_doc, add_record

pytestmark = pytest.mark.integration

D1 = datetime(2026, 1, 1, tzinfo=UTC)
D2 = datetime(2026, 1, 2, tzinfo=UTC)
D3 = datetime(2026, 1, 3, tzinfo=UTC)


def _config() -> KhConfig:
    return KhConfig(
        people={"alice": "alice@example.test", "bob": "bob@example.test"},
        labels={"ember": "Ember", "other": "Other", "unused": "Unused"},
        tech_org="tech",
        tech_repos={"svc": TechRepoConfig(labels=["ember"], dirs=["docs"])},
        groups={
            "all": GroupConfig(members=["alice"], approvers=["alice"]),
            "mgmt": GroupConfig(members=["bob"], approvers=["bob"]),
            "empty": GroupConfig(members=["alice"], approvers=["alice"]),
        },
    )


def _job(conn, state, attempts=0, age="0 minutes", error=None, repo="kb-all"):
    conn.execute(
        "INSERT INTO kh.index_jobs (kind, repo, payload, state, attempts, last_error, created_at)"
        " VALUES ('git_push', %s, '{}'::jsonb, %s, %s, %s, now() - %s::interval)",
        (repo, state, attempts, error, age),
    )


@pytest.fixture
def seeded(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        for name in ("all", "mgmt", "empty"):
            conn.execute("INSERT INTO kh.groups (name, repo) VALUES (%s, %s)", (name, f"kb-{name}"))
        for label in ("ember", "other", "unused"):
            conn.execute("INSERT INTO kh.labels (name) VALUES (%s)", (label,))
        # group all: two T0 documents (one stale) and a T0a document of the tech repo
        add_chunked_doc(conn, "alpha", body="aaaa", stale=True, indexed_at=D1, labels=["ember"])
        add_chunked_doc(conn, "beta", embed=False, body="bb", indexed_at=D2, labels=["ember", "other"])
        add_chunked_doc(conn, "gamma", body="ccc", repo="svc", tier="T0a", indexed_at=D1)
        # group mgmt: one document without chunks
        add_doc(conn, audience="mgmt", repo="kb-mgmt", body="dd", indexed_at=D3)
        # records: two chunked (one outdated), one old without chunks; mgmt: one recent without chunks
        add_chunked_record(conn, "r one", ingested_at=D1, labels=["ember"])
        outdated = add_chunked_record(conn, "r two", ingested_at=D2)
        conn.execute("UPDATE kh.records SET outdated = true, outdated_at = now() WHERE id = %s", (outdated,))
        add_record(conn, ingested_at=D3)
        add_record(conn, audience="mgmt", ingested_at=datetime.now(UTC))
        # jobs: the record triggers queued some of their own; start from a known table
        conn.execute("DELETE FROM kh.index_jobs")
        _job(conn, "queued", age="10 minutes")
        _job(conn, "queued")
        _job(conn, "running")
        _job(conn, "running", age="2 hours")
        _job(conn, "done")
        _job(conn, "failed", attempts=2, error="x" * 400)
        _job(conn, "failed", attempts=MAX_ATTEMPTS, error="dead")
        conn.execute("INSERT INTO kh.index_alerts (repo, path, message) VALUES ('kb-all', 'a.md', 'bad')")
        conn.execute("INSERT INTO kh.repo_state (repo, last_indexed_sha) VALUES ('kb-all', 'sha1')")
        conn.commit()


@pytest.fixture
async def client(indexer_pool, tmp_path):
    cfg_file = tmp_path / "kh.yaml"
    cfg_file.write_text("")
    ctx = PanelContext(
        settings=PanelSettings(env="dev", config_file=cfg_file, dev_admin="admin@example.test"),
        signer=None,
        upstream=None,
        load_config=_config,
        pool=indexer_pool,
    )
    transport = httpx.ASGITransport(app=build_panel_app(ctx))
    async with httpx.AsyncClient(transport=transport, base_url="http://panel.test") as c:
        yield c


async def test_group_stats_numbers(seeded, client):
    body = (await client.get("/api/groups")).json()
    by_name = {g["name"]: g["stats"] for g in body["groups"]}
    assert by_name["all"] == {
        "documents": {"T0": 2, "T0a": 1},
        "stale_documents": 1,
        "records": 3,
        "outdated_records": 1,
        "chunks": {"T0": 2, "T0a": 1, "T3": 2},
        "embedded_chunks": 4,
        "document_chars": 9,
        "last_indexed_at": D2.isoformat(),
        "last_ingested_at": D3.isoformat(),
    }
    mgmt = by_name["mgmt"]
    assert mgmt["documents"] == {"T0": 1, "T0a": 0}
    assert mgmt["document_chars"] == 2
    assert mgmt["last_indexed_at"] == D3.isoformat()
    assert mgmt["records"] == 1 and mgmt["outdated_records"] == 0
    assert mgmt["chunks"] == {"T0": 0, "T0a": 0, "T3": 0} and mgmt["embedded_chunks"] == 0


async def test_group_without_rows_has_zeros(seeded, client):
    body = (await client.get("/api/groups")).json()
    empty = next(g for g in body["groups"] if g["name"] == "empty")
    assert empty["stats"] == {
        "documents": {"T0": 0, "T0a": 0},
        "stale_documents": 0,
        "records": 0,
        "outdated_records": 0,
        "chunks": {"T0": 0, "T0a": 0, "T3": 0},
        "embedded_chunks": 0,
        "document_chars": 0,
        "last_indexed_at": None,
        "last_ingested_at": None,
    }


async def test_labels(seeded, client):
    body = (await client.get("/api/labels")).json()
    rows = {r["name"]: r for r in body["labels"]}
    assert (rows["ember"]["documents"], rows["ember"]["records"]) == (2, 1)
    assert rows["ember"]["tech_repos"] == ["svc"]
    assert (rows["other"]["documents"], rows["other"]["records"]) == (1, 0)
    assert (rows["unused"]["documents"], rows["unused"]["records"]) == (0, 0)


async def test_repos(seeded, client):
    body = (await client.get("/api/repos")).json()
    rows = {r["name"]: r for r in body["repos"]}
    assert rows["kb-all"]["documents"] == 2 and rows["kb-all"]["last_indexed_sha"] == "sha1"
    assert rows["kb-mgmt"]["documents"] == 1 and rows["kb-mgmt"]["last_indexed_sha"] is None
    assert rows["svc"]["documents"] == 1 and rows["svc"]["kind"] == "tech"


async def test_pipeline_numbers(seeded, client):
    body = (await client.get("/api/pipeline")).json()
    assert body["jobs"] == {
        "queued": 2, "running": 2, "done": 1, "failed_retrying": 1, "failed_dead": 1,
    }
    assert 600 <= body["oldest_queued_age_s"] < 700
    assert body["stuck_running"] == 1
    assert body["records_without_chunks"] == 1
    assert [(j["attempts"], j["dead"]) for j in body["failed"]] == [(MAX_ATTEMPTS, True), (2, False)]
    assert len(body["failed"][1]["last_error"]) == 300
    assert [(a["repo"], a["path"], a["message"]) for a in body["alerts"]] == [("kb-all", "a.md", "bad")]
    assert [r["repo"] for r in body["repos"]] == ["kb-all"]


async def test_pipeline_on_empty_database(fresh_db, indexer_pool):
    got = await stats.pipeline(indexer_pool)
    assert got["jobs"] == {"queued": 0, "running": 0, "done": 0, "failed_retrying": 0, "failed_dead": 0}
    assert got["oldest_queued_age_s"] is None
    assert got["failed"] == [] and got["alerts"] == [] and got["repos"] == []


async def test_read_helper_rejects_writes(fresh_db, indexer_pool):
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        async with stats._read(indexer_pool) as conn:
            await conn.execute("INSERT INTO kh.index_alerts (repo, path, message) VALUES ('r', 'p', 'm')")
    async with stats._read(indexer_pool) as conn:
        row = await (await conn.execute("SELECT count(*) FROM kh.index_alerts")).fetchone()
        timeout = await (await conn.execute("SHOW statement_timeout")).fetchone()
    assert row[0] == 0
    assert timeout[0] == "5s"
