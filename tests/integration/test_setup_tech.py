"""``TechStep``: documents of a repo that left ``tech_repos`` are reported and pruned (plan 3.8)."""

import psycopg
import pytest

from knowledge_hub.config import GroupConfig, KhConfig, TechRepoConfig
from knowledge_hub.setup.tech import TechStep
from tests.support.seed import add_chunk, add_doc

pytestmark = pytest.mark.integration

DB = "kh_fresh"


@pytest.fixture
def dsn(fresh_db):
    return fresh_db.dsn("kh_migrator", DB)


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all')")
        yield c


def config(*repos):
    return KhConfig(
        people={"alice": "a@example.test"},
        groups={"all": GroupConfig(members=["alice"], approvers=["alice"])},
        tech_org="kh-tech" if repos else None,
        tech_repos={r: TechRepoConfig() for r in repos},
    )


def seed(conn, repo, tier="T0a"):
    doc = add_doc(conn, repo=repo, tier=tier)
    add_chunk(conn, document_id=doc)
    conn.execute("INSERT INTO kh.repo_state (repo, last_indexed_sha) VALUES (%s, 'x') "
                 "ON CONFLICT DO NOTHING", (repo,))
    return doc


def count(conn, table, where="true", args=()):
    return conn.execute(f"SELECT count(*) FROM kh.{table} WHERE {where}", args).fetchone()[0]


async def test_plan_reports_documents_of_a_repo_not_in_tech_repos(conn, dsn):
    seed(conn, "old-docs")
    seed(conn, "kept")
    out = [c.render() for c in await TechStep(config("kept"), dsn).plan()]
    assert out == ["? tech documents of old-docs: repo is not in tech_repos"]


async def test_apply_without_prune_deletes_nothing(conn, dsn):
    seed(conn, "old-docs")
    out = [c.render() for c in await TechStep(config(), dsn).apply(False)]
    assert out == ["? tech documents of old-docs: repo is not in tech_repos"]
    assert count(conn, "documents") == 1 and count(conn, "chunks") == 1
    assert count(conn, "repo_state", "repo = 'old-docs'") == 1


async def test_apply_prune_deletes_documents_chunks_and_repo_state(conn, dsn):
    seed(conn, "old-docs")
    out = [c.render() for c in await TechStep(config(), dsn).apply(True)]
    assert out == ["- tech documents of old-docs"]
    assert count(conn, "documents") == 0 and count(conn, "chunks") == 0
    assert count(conn, "repo_state") == 0
    assert await TechStep(config(), dsn).plan() == []


async def test_prune_never_touches_a_configured_tech_repo(conn, dsn):
    seed(conn, "kept")
    assert await TechStep(config("kept"), dsn).apply(True) == []
    assert count(conn, "documents") == 1 and count(conn, "chunks") == 1
    assert count(conn, "repo_state", "repo = 'kept'") == 1


async def test_prune_never_touches_t0_documents(conn, dsn):
    seed(conn, "kb-all", tier="T0")
    seed(conn, "old-docs")
    await TechStep(config(), dsn).apply(True)
    assert count(conn, "documents", "tier = 'T0'") == 1
    assert count(conn, "documents", "tier = 'T0a'") == 0
    assert count(conn, "repo_state", "repo = 'kb-all'") == 1
