"""``rebuild`` and ``check`` against FakeForgejo and the migrated database."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.forgejo import ForgejoReader
from knowledge_hub.indexer.rebuild import check, rebuild
from knowledge_hub.indexer.sync import sync_repo
from knowledge_hub.mcp.server import Deps
from tests.support.forgejo_fake import FakeForgejo, md
from tests.support.seed import add_chunk, add_record

DB = "kh_fresh"
CFG = KhConfig(
    people={"alice": "alice@example.test", "bob": "bob@example.test"},
    groups={
        "all": GroupConfig(members=["alice"], approvers=["alice"]),
        "mgmt": GroupConfig(members=["bob"], approvers=["bob"]),
    },
)


@pytest.fixture
def world(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        for name in CFG.groups:
            conn.execute(
                "INSERT INTO kh.groups (name, repo) VALUES (%s, %s)", (name, CFG.repo_for_group(name))
            )
    return fresh_db


@pytest.fixture
def fake():
    f = FakeForgejo()
    f.add_repo("kb-all")
    f.add_repo("kb-mgmt")
    return f


@pytest.fixture
async def reader(fake):
    r = ForgejoReader("https://forgejo.test", "tok", "kh", transport=fake.transport)
    yield r
    await r.aclose()


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool, reader):
    return Deps(
        settings=app_settings, config=CFG, app_pool=app_pool, indexer_pool=indexer_pool,
        embedder=FakeEmbedder(1024), reader=reader,
    )  # fmt: skip


def q(world, sql, *args):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        cur = conn.execute(sql, args)
        return cur.fetchall() if cur.description else []


async def synced(deps, fake):
    fake.commit("kb-all", {"a.md": md("a", "A"), "b.md": md("b", "B")})
    fake.commit("kb-mgmt", {"m.md": md("m", "M")})
    for repo in ("kb-all", "kb-mgmt"):
        await sync_repo(deps.indexer_pool, deps.reader, CFG, repo, deps.embedder, chunker_version=1)


async def test_clean_index_has_no_drift(world, fake, deps):
    await synced(deps, fake)
    assert await check(deps) == []


async def test_never_indexed_repo_is_drift(world, fake, deps):
    head = fake.head("kb-all")
    got = await check(deps)
    assert f"head of kb-all is {head}, indexed nothing" in got


async def test_head_moved(world, fake, deps):
    await synced(deps, fake)
    old = fake.head("kb-all")
    new = fake.commit("kb-all", {"c.md": md("c", "C")})
    assert await check(deps) == [f"head of kb-all is {new}, indexed {old}"]


async def test_document_without_chunks(world, fake, deps):
    await synced(deps, fake)
    q(world, "DELETE FROM kh.chunks WHERE document_id = (SELECT id FROM kh.documents WHERE path = 'a.md')")
    assert await check(deps) == ["document a.md in kb-all has no chunks"]


async def test_record_without_chunks_only_after_five_minutes(world, fake, deps):
    await synced(deps, fake)
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        old = add_record(conn)
        add_record(conn)  # just ingested: still waiting for its job
        covered = add_record(conn)
        add_chunk(conn, record_id=covered, tier="T3")
        conn.execute(
            "UPDATE kh.records SET ingested_at = now() - interval '6 minutes' WHERE id IN (%s, %s)",
            (old, covered),
        )
    assert await check(deps) == [f"record {old} has no chunks"]


async def test_audience_drift(world, fake, deps):
    await synced(deps, fake)
    q(world, "UPDATE kh.documents SET audience = 'mgmt' WHERE path = 'a.md'")
    assert await check(deps) == ["document a.md in kb-all has audience mgmt, config says all"]


async def test_rebuild_one_repo_clears_state_and_syncs(world, fake, deps):
    await synced(deps, fake)
    fake.commit("kb-all", {"c.md": md("c", "C")})
    done = await rebuild(deps, "kb-all")
    assert list(done) == ["kb-all"]
    assert done["kb-all"].head_sha == fake.head("kb-all")
    assert q(world, "SELECT repo, last_indexed_sha FROM kh.repo_state ORDER BY repo") == [
        ("kb-all", fake.head("kb-all")),
        ("kb-mgmt", fake.head("kb-mgmt")),
    ]
    assert len(q(world, "SELECT 1 FROM kh.documents WHERE repo = 'kb-all'")) == 3


async def test_rebuild_every_repo_after_a_rewrite_raises_no_alert(world, fake, deps):
    await synced(deps, fake)
    fake.rewrite("kb-all", {"z.md": md("z", "Z")})
    done = await rebuild(deps)
    assert sorted(done) == ["kb-all", "kb-mgmt"]
    assert q(world, "SELECT path FROM kh.documents WHERE repo = 'kb-all'") == [("z.md",)]
    assert q(world, "SELECT 1 FROM kh.index_alerts") == []
    assert await check(deps) == []


async def test_rebuild_refuses_unknown_repo_and_missing_reader(world, fake, deps):
    with pytest.raises(ValueError, match="in no group"):
        await rebuild(deps, "nope")
    from dataclasses import replace

    with pytest.raises(ValueError, match="KH_FORGEJO_URL"):
        await rebuild(replace(deps, reader=None))
    with pytest.raises(ValueError, match="KH_FORGEJO_URL"):
        await check(replace(deps, reader=None))
