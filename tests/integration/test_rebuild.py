"""``rebuild`` and ``check`` (plan section 3.6) against PostgreSQL with the Forgejo fake."""

from __future__ import annotations

from types import SimpleNamespace

import psycopg

from knowledge_hub.config import parse_config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.rebuild import check, config_hash, rebuild
from knowledge_hub.mcp.server import Deps
from tests.support.forgejo_fake import ForgejoFake
from tests.unit.test_sync_plan import doc, merged, repo_of

DB = "kh_fresh"
CFG = parse_config({"repos": {"canonical-all": {"tier": "T0", "audience": "all", "trust_anchor": "a0"}}})


def rows(fresh_db, sql, *args):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


def make_deps(indexer_pool, repo_obj, config=CFG):
    return Deps(
        settings=SimpleNamespace(chunker_version=1),
        config=config,
        app_pool=None,
        indexer_pool=indexer_pool,
        embedder=FakeEmbedder(),
        forgejo=ForgejoFake({"canonical-all": repo_obj}),
    )


def two_docs():
    return repo_of([merged("m1", "a0", {"a.md": doc("a"), "b.md": doc("b")}, 1)])


SNAP = (
    "SELECT d.id, d.doc_key, d.content_sha, d.indexed_at, c.id, c.content_sha, c.ordinal"
    " FROM kh.documents d LEFT JOIN kh.chunks c ON c.document_id = d.id ORDER BY 2, 7"
)


async def test_rebuild_twice_changes_nothing(fresh_db, indexer_pool):
    deps = make_deps(indexer_pool, two_docs())
    assert await rebuild(deps) == ["canonical-all"]
    first = rows(fresh_db, SNAP)
    assert len(first) >= 2
    await rebuild(deps, "canonical-all")
    assert rows(fresh_db, SNAP) == first


async def test_rebuild_removes_deleted_document(fresh_db, indexer_pool):
    await rebuild(make_deps(indexer_pool, two_docs()))
    gone = repo_of(
        [
            merged("m1", "a0", {"a.md": doc("a"), "b.md": doc("b")}, 1),
            merged("m2", "m1", {"b.md": None}, 2),
        ]
    )
    await rebuild(make_deps(indexer_pool, gone))
    assert rows(fresh_db, "SELECT doc_key FROM kh.documents") == [("a",)]
    assert rows(
        fresh_db,
        "SELECT count(*) FROM kh.chunks c LEFT JOIN kh.documents d ON d.id = c.document_id"
        " WHERE d.id IS NULL",
    ) == [(0,)]


async def test_check_clean_then_drift(fresh_db, indexer_pool):
    deps = make_deps(indexer_pool, two_docs())
    await rebuild(deps)
    # The config row is written by recompute-visibility and the startup load, not by rebuild.
    assert (await check(deps)).config_drift
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO kh.repo_state (repo, config_hash) VALUES ('__config__', %s)", (config_hash(CFG),)
        )
    report = await check(deps)
    assert report.clean, report.lines()

    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        # The inherit trigger would repair the edit at once; switch it off to simulate drift.
        conn.execute("ALTER TABLE kh.chunks DISABLE TRIGGER chunks_inherit_owner")
        conn.execute("UPDATE kh.chunks SET entities = '{x}' WHERE id = (SELECT id FROM kh.chunks LIMIT 1)")
        conn.execute("ALTER TABLE kh.chunks ENABLE TRIGGER chunks_inherit_owner")
    report = await check(deps)
    assert len(report.chunk_drift) == 1 and not report.clean
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("UPDATE kh.chunks SET id = id")  # fires the trigger again: repaired
    assert (await check(deps)).clean


async def test_check_reports_config_hash_change(fresh_db, indexer_pool):
    deps = make_deps(indexer_pool, two_docs())
    await rebuild(deps)
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO kh.repo_state (repo, config_hash) VALUES ('__config__', %s)", (config_hash(CFG),)
        )
    assert (await check(deps)).clean
    other = parse_config(
        {
            "repos": {"canonical-all": {"tier": "T0", "audience": "all", "trust_anchor": "a0"}},
            "raw_audience_map": {"series:x": ["all"]},
        }
    )
    assert config_hash(other) != config_hash(CFG)
    deps.config = other
    report = await check(deps)
    assert report.config_drift and not report.clean


async def test_check_reports_head_and_content_drift(fresh_db, indexer_pool):
    deps = make_deps(indexer_pool, two_docs())
    await rebuild(deps)
    newer = repo_of(
        [
            merged("m1", "a0", {"a.md": doc("a"), "b.md": doc("b")}, 1),
            merged("m2", "m1", {"a.md": doc("a", title="Changed")}, 2),
        ]
    )
    deps.forgejo = ForgejoFake({"canonical-all": newer})
    report = await check(deps)
    assert report.repo_drift == ["canonical-all"]
    assert report.document_drift == ["canonical-all/a.md"]


async def test_rebuild_twice_with_offset_dates_changes_nothing(fresh_db, indexer_pool):
    c, p, r = merged("m1", "a0", {"b.md": doc("b")}, 1)
    r[0]["submitted_at"] = "2026-11-02T11:00:00.750+01:00"
    repo = repo_of([(c, p, r)], {"a.md": doc("a")})
    repo.commits[0].date = "2026-11-01T11:00:00+01:00"
    deps = make_deps(indexer_pool, repo)
    await rebuild(deps)
    first = rows(fresh_db, SNAP)
    await rebuild(deps, "canonical-all")
    assert rows(fresh_db, SNAP) == first


async def test_check_reports_chunk_agent_written_drift(fresh_db, indexer_pool):
    deps = make_deps(indexer_pool, two_docs())
    await rebuild(deps)
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("ALTER TABLE kh.chunks DISABLE TRIGGER chunks_inherit_owner")
        conn.execute(
            "UPDATE kh.chunks SET agent_written = true WHERE id = (SELECT id FROM kh.chunks LIMIT 1)"
        )
        conn.execute("ALTER TABLE kh.chunks ENABLE TRIGGER chunks_inherit_owner")
    assert len((await check(deps)).chunk_drift) == 1
