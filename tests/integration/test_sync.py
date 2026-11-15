"""Integration tests of the sync write (plan section 3.6) against PostgreSQL with the Forgejo fake."""

from __future__ import annotations

import psycopg

from knowledge_hub.config import parse_config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.sync import sync_repo
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake
from tests.unit.test_sync_plan import doc, merged, repo_of

DB = "kh_fresh"
CFG = parse_config({"repos": {"canonical-all": {"tier": "T0", "audience": "all", "trust_anchor": "a0"}}})


def rows(fresh_db, sql, *args):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


async def sync(pool, repo_obj):
    return await sync_repo(
        pool, ForgejoFake({"canonical-all": repo_obj}), CFG, "canonical-all", FakeEmbedder()
    )


async def test_approval_fields_and_repo_state(fresh_db, indexer_pool):
    r = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1)])
    await sync(indexer_pool, r)
    (row,) = rows(
        fresh_db,
        "SELECT status, approved_by, approved_at, merge_pr, pr_url, commit_sha, review_by, required_audiences"
        " FROM kh.documents",
    )
    assert row[0] == "current" and row[1] == ["bob"] and row[3] == 1
    assert row[2].isoformat() == "2026-11-02T10:00:00+00:00"
    assert row[4].endswith("/pulls/1") and row[5] == "m1" and row[7] == ["all"]
    assert rows(fresh_db, "SELECT trusted, last_indexed_sha, trust_anchor_sha FROM kh.repo_state") == [
        (True, "m1", "a0")
    ]
    assert rows(fresh_db, "SELECT commit_sha FROM kh.document_versions") == [("m1",)]


async def test_disputed_file_has_alert_once(fresh_db, indexer_pool):
    r = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1, approve=False)])
    await sync(indexer_pool, r)
    await sync(indexer_pool, r)
    assert rows(fresh_db, "SELECT status, approved_by FROM kh.documents") == [("disputed", [])]
    assert rows(fresh_db, "SELECT path, message FROM kh.index_alerts") == [
        ("a.md", "file a.md on canonical-all/main has no approved merge PR")
    ]


async def test_rename_keeps_document_id(fresh_db, indexer_pool):
    r1 = repo_of([merged("m1", "a0", {"old.md": doc("a")}, 1)])
    await sync(indexer_pool, r1)
    (before,) = rows(fresh_db, "SELECT id FROM kh.documents")
    r2 = repo_of(
        [
            merged("m1", "a0", {"old.md": doc("a")}, 1),
            merged("m2", "m1", {"old.md": None, "new.md": doc("a")}, 2),
        ]
    )
    await sync(indexer_pool, r2)
    assert rows(fresh_db, "SELECT id, path FROM kh.documents") == [(before[0], "new.md")]
    assert rows(fresh_db, "SELECT count(*) FROM kh.document_versions") == [(2,)]


async def test_deletion_removes_document(fresh_db, indexer_pool):
    r1 = repo_of([merged("m1", "a0", {"a.md": doc("a"), "b.md": doc("b")}, 1)])
    await sync(indexer_pool, r1)
    r2 = repo_of(
        [
            merged("m1", "a0", {"a.md": doc("a"), "b.md": doc("b")}, 1),
            merged("m2", "m1", {"b.md": None}, 2),
        ]
    )
    await sync(indexer_pool, r2)
    assert rows(fresh_db, "SELECT doc_key FROM kh.documents") == [("a",)]


async def test_same_head_twice_keeps_indexed_at(fresh_db, indexer_pool):
    r = repo_of([merged("m1", "a0", {"a.md": doc("a"), "n.md": doc("n", "supersedes: a\n")}, 1)])
    await sync(indexer_pool, r)
    first = rows(
        fresh_db, "SELECT doc_key, indexed_at, updated_at, superseded_by FROM kh.documents ORDER BY 1"
    )
    await sync(indexer_pool, r)
    assert (
        rows(fresh_db, "SELECT doc_key, indexed_at, updated_at, superseded_by FROM kh.documents ORDER BY 1")
        == first
    )
    (a_id,) = rows(fresh_db, "SELECT id FROM kh.documents WHERE doc_key = 'n'")
    assert first[0][3] == a_id[0] and first[0][0] == "a"


async def test_rejected_frontmatter_leaves_no_row(fresh_db, indexer_pool):
    r = repo_of([merged("m1", "a0", {"a.md": doc("a", "tier: T0\n")}, 1)])
    await sync(indexer_pool, r)
    assert rows(fresh_db, "SELECT count(*) FROM kh.documents") == [(0,)]
    assert rows(fresh_db, "SELECT count(*) FROM kh.index_alerts") == [(1,)]


async def test_rewritten_main_changes_nothing_but_alerts(fresh_db, indexer_pool):
    r1 = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1)])
    await sync(indexer_pool, r1)
    before = rows(fresh_db, "SELECT * FROM kh.documents")
    rewritten = FakeRepo([FakeCommit("a0", []), FakeCommit("z9", ["a0"], {"a.md": doc("a", title="X")})])
    plan = await sync(indexer_pool, rewritten)
    assert plan.refused
    assert rows(fresh_db, "SELECT * FROM kh.documents") == before
    assert rows(fresh_db, "SELECT last_indexed_sha FROM kh.repo_state") == [("m1",)]
    assert rows(fresh_db, "SELECT message FROM kh.index_alerts") == [
        ("main of canonical-all was rewritten; run kh rebuild after review",)
    ]
