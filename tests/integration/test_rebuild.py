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
    assert await check(deps) == [
        "document a.md in kb-all has audience mgmt, config says all; run kh rebuild --repo kb-all"
    ]


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


# --- tech repos ------------------------------------------------------------------------------

from dataclasses import replace  # noqa: E402

from tests.support.tech import TECH_CONFIG, TECH_REPO  # noqa: E402


@pytest.fixture
def tech_fake():
    f = FakeForgejo(org="kh-tech")
    f.add_repo(TECH_REPO, branch="trunk")
    f.commit(TECH_REPO, {"docs/a.md": "# Alpha\n\nText.\n"})
    return f


@pytest.fixture
async def tech_reader(tech_fake):
    r = ForgejoReader("https://forgejo.test", "tok", "kh-tech", transport=tech_fake.transport)
    yield r
    await r.aclose()


@pytest.fixture
def tdeps(app_settings, app_pool, indexer_pool, reader, tech_reader):
    return Deps(
        settings=app_settings, config=TECH_CONFIG, app_pool=app_pool, indexer_pool=indexer_pool,
        embedder=FakeEmbedder(1024), reader=reader, tech_reader=tech_reader,
    )  # fmt: skip


@pytest.fixture
def tworld(world, fake):
    for name in TECH_CONFIG.groups:
        q(world, "INSERT INTO kh.groups (name, repo) VALUES (%s, %s) ON CONFLICT DO NOTHING",
          name, TECH_CONFIG.repo_for_group(name))  # fmt: skip
    fake.add_repo("kb-hr")
    q(world, "INSERT INTO kh.labels (name) VALUES ('ember')")
    return world


async def tech_synced(tdeps, fake):
    fake.commit("kb-all", {"a.md": md("a", "A")})
    fake.commit("kb-mgmt", {"m.md": md("m", "M")})
    fake.commit("kb-hr", {"h.md": md("h", "H")})
    for repo in ("kb-all", "kb-mgmt", "kb-hr"):
        await sync_repo(
            tdeps.indexer_pool, tdeps.reader, TECH_CONFIG, repo, tdeps.embedder, chunker_version=1
        )
    await sync_repo(
        tdeps.indexer_pool, None, TECH_CONFIG, TECH_REPO, tdeps.embedder,
        chunker_version=1, tech_reader=tdeps.tech_reader,
    )  # fmt: skip


async def test_rebuild_all_includes_tech_repos(tworld, fake, tdeps):
    await tech_synced(tdeps, fake)
    done = await rebuild(tdeps)
    assert sorted(done) == sorted(["kb-all", "kb-hr", "kb-mgmt", TECH_REPO])
    assert q(tworld, "SELECT tier FROM kh.documents WHERE repo = %s", TECH_REPO) == [("T0a",)]


async def test_rebuild_one_tech_repo(tworld, fake, tech_fake, tdeps):
    await tech_synced(tdeps, fake)
    head = tech_fake.commit(TECH_REPO, {"docs/b.md": "# Beta\n\nMore.\n"})
    done = await rebuild(tdeps, TECH_REPO)
    assert list(done) == [TECH_REPO]
    assert done[TECH_REPO].head_sha == head
    assert q(tworld, "SELECT last_indexed_sha FROM kh.repo_state WHERE repo = %s", TECH_REPO) == [(head,)]
    assert len(q(tworld, "SELECT 1 FROM kh.documents WHERE repo = %s", TECH_REPO)) == 2


async def test_check_reports_tech_head_drift(tworld, fake, tech_fake, tdeps):
    await tech_synced(tdeps, fake)
    assert await check(tdeps) == []
    old = tech_fake.head(TECH_REPO, "trunk")
    new = tech_fake.commit(TECH_REPO, {"docs/b.md": "# B\n"})
    assert await check(tdeps) == [f"head of {TECH_REPO} is {new}, indexed {old}"]


async def test_check_survives_an_unreadable_tech_repo(tworld, fake, tech_fake, tdeps):
    await tech_synced(tdeps, fake)
    q(tworld, "UPDATE kh.documents SET audience = 'mgmt' WHERE repo = 'kb-all'")
    tech_fake.fail_next(f"/repos/kh-tech/{TECH_REPO}", 500)
    got = await check(tdeps)
    assert got[0].startswith(f"tech repo {TECH_REPO} not readable: ")
    assert (
        "document a.md in kb-all has audience mgmt, config says all; run kh rebuild --repo kb-all" in got
    )  # the loop went on


async def test_check_accepts_audience_all_for_tech_documents(tworld, fake, tdeps):
    await tech_synced(tdeps, fake)
    assert q(tworld, "SELECT audience, tier FROM kh.documents WHERE repo = %s", TECH_REPO) == [("all", "T0a")]
    assert await check(tdeps) == []


async def test_check_flags_tier_mismatch(tworld, fake, tdeps):
    await tech_synced(tdeps, fake)
    q(tworld, "UPDATE kh.documents SET tier = 'T0' WHERE repo = %s", TECH_REPO)
    q(tworld, "UPDATE kh.documents SET tier = 'T0a' WHERE path = 'a.md'")
    assert await check(tdeps) == [
        f"document docs/a.md in {TECH_REPO} has tier T0, config says T0a",
        "document a.md in kb-all has tier T0a, config says T0",
    ]


async def test_check_flags_documents_of_a_removed_tech_repo(tworld, fake, tdeps):
    await tech_synced(tdeps, fake)
    no_tech = TECH_CONFIG.model_copy(update={"tech_repos": {}})
    got = await check(replace(tdeps, config=no_tech, tech_reader=None))
    assert got == [
        f"document docs/a.md in {TECH_REPO} has audience all, config says no group",
        f"document docs/a.md in {TECH_REPO} has tier T0a, config says T0",
    ]


async def test_check_without_a_tech_reader_raises_when_tech_repos_are_configured(tworld, fake, tdeps):
    with pytest.raises(ValueError, match="KH_FORGEJO_URL"):
        await check(replace(tdeps, tech_reader=None))
