"""Integration tests of syncing a tech repo (T0a): FakeForgejo into the migrated database."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.forgejo import ForgejoError, ForgejoReader
from knowledge_hub.indexer.sync import sync_repo
from tests.support.forgejo_fake import FakeForgejo, md
from tests.support.tech import TECH_CONFIG, TECH_REPO

DB = "kh_fresh"
CFG = TECH_CONFIG


@pytest.fixture
def world(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        for name in CFG.groups:
            conn.execute(
                "INSERT INTO kh.groups (name, repo) VALUES (%s, %s)", (name, CFG.repo_for_group(name))
            )
        conn.execute("INSERT INTO kh.labels (name) VALUES ('ember')")
    return fresh_db


@pytest.fixture
def fake():
    f = FakeForgejo(org="kh-tech")
    f.add_repo(TECH_REPO, branch="trunk")
    return f


@pytest.fixture
async def reader(fake):
    r = ForgejoReader("https://forgejo.test", "tok", "kh-tech", transport=fake.transport)
    yield r
    await r.aclose()


def q(world, sql, *args):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


async def sync(pool, reader, repo=TECH_REPO, config=CFG):
    return await sync_repo(
        pool, None, config, repo, FakeEmbedder(1024), chunker_version=1, tech_reader=reader
    )


async def test_tech_repo_indexes_docs_and_root_claude_md_only(world, fake, reader, indexer_pool):
    fake.commit(
        TECH_REPO,
        {
            "CLAUDE.md": "Rules.\n",
            "README.md": "x",
            "docs/a.md": "# A\n",
            "docs/sub/b.md": "# B\n",
            "docs/c.txt": "x",
            "src/CLAUDE.md": "x",
            "docs/.hidden/d.md": "x",
        },
    )
    await sync(indexer_pool, reader)
    paths = [r[0] for r in q(world, "SELECT path FROM kh.documents ORDER BY path")]
    assert paths == ["CLAUDE.md", "docs/a.md", "docs/sub/b.md"]


async def test_tech_documents_have_tier_audience_labels_and_key(world, fake, reader, indexer_pool):
    head = fake.commit(TECH_REPO, {"docs/a.md": "# Alpha\n\nText here.\n"})
    plan = await sync(indexer_pool, reader)
    assert plan.head_sha == head
    assert q(world, "SELECT tier, audience, labels, doc_key, title, stale FROM kh.documents") == [
        ("T0a", "all", ["ember"], "docs/a.md", "Alpha", False)
    ]
    assert q(world, "SELECT DISTINCT tier, audience FROM kh.chunks") == [("T0a", "all")]
    assert q(world, "SELECT last_indexed_sha FROM kh.repo_state WHERE repo = %s", TECH_REPO) == [(head,)]


async def test_tech_sync_follows_the_default_branch(world, fake, reader, indexer_pool):
    fake.commit(TECH_REPO, {"docs/a.md": "# A\n"})
    fake.repos[TECH_REPO].branches["main"] = fake.head(TECH_REPO, "trunk")
    fake.commit(TECH_REPO, {"docs/b.md": "# B\n"}, branch="main")
    await sync(indexer_pool, reader)
    assert [r[0] for r in q(world, "SELECT path FROM kh.documents")] == ["docs/a.md"]
    fake.repos[TECH_REPO].default_branch = "main"
    await sync(indexer_pool, reader)
    assert [r[0] for r in q(world, "SELECT path FROM kh.documents ORDER BY path")] == [
        "docs/a.md",
        "docs/b.md",
    ]


async def test_tech_sync_deletes_a_removed_file(world, fake, reader, indexer_pool):
    fake.commit(TECH_REPO, {"docs/a.md": "# A\n\nText a.\n", "docs/b.md": "# B\n\nText b.\n"})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT count(DISTINCT document_id) FROM kh.chunks") == [(2,)]
    fake.commit(TECH_REPO, {"docs/b.md": None})
    await sync(indexer_pool, reader)
    assert [r[0] for r in q(world, "SELECT path FROM kh.documents")] == ["docs/a.md"]
    assert q(world, "SELECT count(DISTINCT document_id) FROM kh.chunks") == [(1,)]


async def test_label_change_in_config_updates_unchanged_files_and_their_chunks(
    world, fake, reader, indexer_pool
):
    fake.commit(TECH_REPO, {"docs/a.md": "# A\n\nText.\n"})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT DISTINCT labels FROM kh.chunks") == [(["ember"],)]
    tech = CFG.tech_repos[TECH_REPO].model_copy(update={"labels": []})
    cfg = CFG.model_copy(update={"tech_repos": {TECH_REPO: tech}})
    await sync(indexer_pool, reader, config=cfg)
    assert q(world, "SELECT labels FROM kh.documents") == [([],)]
    assert q(world, "SELECT DISTINCT labels FROM kh.chunks") == [([],)]


async def test_force_pushed_head_raises_no_alert(world, fake, reader, indexer_pool):
    fake.commit(TECH_REPO, {"docs/a.md": "# A\n"})
    await sync(indexer_pool, reader)
    fake.rewrite(TECH_REPO, {"docs/a.md": "# A2\n"}, branch="trunk")
    await sync(indexer_pool, reader)
    assert q(world, "SELECT title FROM kh.documents") == [("A2",)]
    assert q(world, "SELECT count(*) FROM kh.index_alerts") == [(0,)]


async def test_frontmatter_of_a_tech_file_is_not_indexed(world, fake, reader, indexer_pool):
    fake.commit(TECH_REPO, {"docs/a.md": "---\nid: x\nreview_by: nonsense\n---\n# Real\n\nBody.\n"})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT title, body FROM kh.documents") == [("Real", "# Real\n\nBody.\n")]
    assert q(world, "SELECT count(*) FROM kh.index_alerts") == [(0,)]


async def test_non_utf8_file_is_skipped_with_an_alert(world, fake, reader, indexer_pool, monkeypatch):
    fake.commit(TECH_REPO, {"docs/a.md": "# A\n", "docs/b.md": "# B\n"})
    real = reader.get_raw

    async def get_raw(repo, path, sha):
        if path == "docs/a.md":
            raise ForgejoError("GET raw docs/a.md failed: not UTF-8")
        return await real(repo, path, sha)

    monkeypatch.setattr(reader, "get_raw", get_raw)
    await sync(indexer_pool, reader)
    assert [r[0] for r in q(world, "SELECT path FROM kh.documents")] == ["docs/b.md"]
    assert q(world, "SELECT path, message FROM kh.index_alerts") == [
        ("docs/a.md", "file skipped: not UTF-8")
    ]


async def test_label_missing_from_kh_labels_is_alerted_not_fatal(world, fake, reader, indexer_pool):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("DELETE FROM kh.labels")
    fake.commit(TECH_REPO, {"docs/a.md": "# A\n"})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT labels FROM kh.documents") == [([],)]
    assert q(world, "SELECT path, message FROM kh.index_alerts") == [
        ("", "label ember is not in kh.labels; run kh setup apply")
    ]


async def test_group_repo_sync_is_unchanged(world, fake, indexer_pool):
    group_fake = FakeForgejo(org="kh")
    group_fake.add_repo("kb-all")
    group_fake.commit("kb-all", {"a.md": md("a", "A")})
    r = ForgejoReader("https://forgejo.test", "tok", "kh", transport=group_fake.transport)
    try:
        await sync_repo(indexer_pool, r, CFG, "kb-all", FakeEmbedder(1024), chunker_version=1)
    finally:
        await r.aclose()
    assert q(world, "SELECT tier, audience, title FROM kh.documents") == [("T0", "all", "A")]
    assert ("GET", "/api/v1/repos/kh/kb-all") not in group_fake.requests
