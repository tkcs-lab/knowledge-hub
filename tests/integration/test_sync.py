"""Integration tests of ``sync_repo``: FakeForgejo into the migrated database."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.forgejo import ForgejoReader
from knowledge_hub.indexer.sync import sync_repo
from tests.support.forgejo_fake import FakeForgejo, md

DB = "kh_fresh"
CFG = KhConfig(
    people={"alice": "alice@example.test", "bob": "bob@example.test"},
    groups={
        "all": GroupConfig(members=["alice"], approvers=["alice"]),
        "mgmt": GroupConfig(members=["bob"], approvers=["bob"]),
    },
)
REWRITE_ALERT = "main of kb-all was rewritten; run kh rebuild after review"


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
    f = FakeForgejo()
    f.add_repo("kb-all")
    f.add_repo("kb-mgmt")
    return f


@pytest.fixture
async def reader(fake):
    r = ForgejoReader("https://forgejo.test", "tok", "kh", transport=fake.transport)
    yield r
    await r.aclose()


def q(world, sql, *args):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


def docs(world, repo="kb-all"):
    return q(world, "SELECT path, doc_key, title FROM kh.documents WHERE repo = %s ORDER BY path", repo)


async def sync(pool, reader, repo="kb-all"):
    return await sync_repo(pool, reader, CFG, repo, FakeEmbedder(1024), chunker_version=1)


async def test_first_sync(world, fake, reader, indexer_pool):
    head = fake.commit(
        "kb-all", {"a.md": md("a", "A"), "docs/b.md": md("b", "B", labels=["ember"]), "README.md": "x"}
    )
    await sync(indexer_pool, reader)
    assert docs(world) == [("a.md", "a", "A"), ("docs/b.md", "b", "B")]
    got = q(world, "SELECT audience, commit_sha, blob_sha <> '', labels FROM kh.documents ORDER BY path")
    assert got == [("all", head, True, []), ("all", head, True, ["ember"])]
    assert q(world, "SELECT repo, last_indexed_sha FROM kh.repo_state") == [("kb-all", head)]
    assert q(world, "SELECT count(*) FROM kh.index_alerts") == [(0,)]


async def test_edit_updates_the_row_in_place(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("a", "A")})
    await sync(indexer_pool, reader)
    [(doc_id,)] = q(world, "SELECT id FROM kh.documents")
    head = fake.commit("kb-all", {"a.md": md("a", "A2", body="Changed.\n")})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT id, title, body, commit_sha FROM kh.documents") == [
        (doc_id, "A2", "Changed.\n", head)
    ]


async def test_unchanged_file_keeps_its_old_commit_sha(world, fake, reader, indexer_pool):
    first = fake.commit("kb-all", {"a.md": md("a")})
    await sync(indexer_pool, reader)
    fake.commit("kb-all", {"b.md": md("b")})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT path, commit_sha FROM kh.documents ORDER BY path")[0] == ("a.md", first)


async def test_delete_removes_row_and_chunks(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("a"), "b.md": md("b")})
    await sync(indexer_pool, reader)
    fake.commit("kb-all", {"a.md": None})
    await sync(indexer_pool, reader)
    assert [d[0] for d in docs(world)] == ["b.md"]
    assert q(world, "SELECT count(*) FROM kh.chunks") == [(1,)]


async def test_rename_keeps_the_row_and_its_chunks(world, fake, reader, indexer_pool):
    text = md("a", body="# One\nalpha text.\n")
    fake.commit("kb-all", {"a.md": text})
    await sync(indexer_pool, reader)
    before = q(world, "SELECT d.id, c.id FROM kh.documents d JOIN kh.chunks c ON c.document_id = d.id")
    fake.commit("kb-all", {"a.md": None, "moved/a.md": text})
    await sync(indexer_pool, reader)
    assert docs(world) == [("moved/a.md", "a", "T")]
    assert (
        q(world, "SELECT d.id, c.id FROM kh.documents d JOIN kh.chunks c ON c.document_id = d.id") == before
    )


async def test_path_swap_of_two_documents(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("a"), "b.md": md("b")})
    await sync(indexer_pool, reader)
    fake.commit("kb-all", {"a.md": md("b"), "b.md": md("a")})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT path, doc_key FROM kh.documents ORDER BY path") == [("a.md", "b"), ("b.md", "a")]


async def test_changed_id_at_the_same_path(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("old")})
    await sync(indexer_pool, reader)
    fake.commit("kb-all", {"a.md": md("new")})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT path, doc_key FROM kh.documents") == [("a.md", "new")]


async def test_unknown_label_then_label_added(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("a", labels=["later"]), "ok.md": md("ok")})
    await sync(indexer_pool, reader)
    assert [d[0] for d in docs(world)] == ["ok.md"]
    alerts = "SELECT path, message FROM kh.index_alerts WHERE repo = 'kb-all'"
    assert q(world, alerts) == [("a.md", "file rejected: unknown label later")]
    await sync(indexer_pool, reader)  # retried, the alert is not repeated
    assert q(world, alerts) == [("a.md", "file rejected: unknown label later")]
    q(world, "INSERT INTO kh.labels (name) VALUES ('later') RETURNING name")
    await sync(indexer_pool, reader)
    assert [d[0] for d in docs(world)] == ["a.md", "ok.md"]
    assert q(world, "SELECT labels FROM kh.documents WHERE path = 'a.md'") == [(["later"],)]


async def test_duplicate_id_rejects_both_and_deleting_one_restores_the_other(
    world, fake, reader, indexer_pool
):
    fake.commit("kb-all", {"a.md": md("same", "A"), "b.md": md("same", "B")})
    await sync(indexer_pool, reader)
    assert docs(world) == []
    got = q(world, "SELECT path, message FROM kh.index_alerts ORDER BY path")
    assert got == [("a.md", "file rejected: duplicate id same"), ("b.md", "file rejected: duplicate id same")]
    fake.commit("kb-all", {"b.md": None})
    await sync(indexer_pool, reader)
    assert docs(world) == [("a.md", "same", "A")]


async def test_new_duplicate_removes_the_stored_holder(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("same", "A")})
    await sync(indexer_pool, reader)
    fake.commit("kb-all", {"b.md": md("same", "B")})
    await sync(indexer_pool, reader)
    assert docs(world) == []
    assert q(world, "SELECT count(*) FROM kh.chunks") == [(0,)]


async def test_rewrite_alerts_and_carries_on(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("a", "A")})
    await sync(indexer_pool, reader)
    new = fake.rewrite("kb-all", {"a.md": md("a", "Rewritten"), "c.md": md("c")})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT path, message FROM kh.index_alerts") == [("", REWRITE_ALERT)]
    assert [d[2] for d in docs(world)] == ["Rewritten", "T"]
    assert q(world, "SELECT last_indexed_sha FROM kh.repo_state") == [(new,)]
    fake.rewrite("kb-all", {"a.md": md("a", "Again")})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT count(*) FROM kh.index_alerts") == [(1,)]  # same message is not stored twice


async def test_normal_push_gives_no_rewrite_alert(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("a")})
    await sync(indexer_pool, reader)
    fake.commit("kb-all", {"b.md": md("b")})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT count(*) FROM kh.index_alerts") == [(0,)]


async def test_audience_equals_the_group_of_the_repo(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("a")})
    fake.commit("kb-mgmt", {"m.md": md("m")})
    await sync(indexer_pool, reader, "kb-all")
    await sync(indexer_pool, reader, "kb-mgmt")
    assert q(world, "SELECT repo, audience FROM kh.documents ORDER BY repo") == [
        ("kb-all", "all"),
        ("kb-mgmt", "mgmt"),
    ]
    assert q(
        world,
        "SELECT d.repo, c.audience FROM kh.chunks c JOIN kh.documents d ON d.id = c.document_id"
        " ORDER BY d.repo",
    ) == [("kb-all", "all"), ("kb-mgmt", "mgmt")]


async def test_stale_flag_follows_review_by(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"old.md": md("old", review_by="2000-01-01"), "new.md": md("new")})
    await sync(indexer_pool, reader)
    assert q(world, "SELECT doc_key, stale FROM kh.documents ORDER BY doc_key") == [
        ("new", False),
        ("old", True),
    ]


async def test_repo_in_no_group_is_a_value_error(world, fake, reader, indexer_pool):
    fake.add_repo("stray")
    with pytest.raises(ValueError, match="no group"):
        await sync(indexer_pool, reader, "stray")


async def test_failed_sync_leaves_state_untouched(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("a")})
    fake.fail_next("/raw/", 500)
    with pytest.raises(Exception, match="500"):
        await sync(indexer_pool, reader)
    assert docs(world) == [] and q(world, "SELECT count(*) FROM kh.repo_state") == [(0,)]
    await sync(indexer_pool, reader)
    assert len(docs(world)) == 1


def sup(world, key="old"):
    [(v,)] = q(world, "SELECT superseded_by::text FROM kh.documents WHERE doc_key = %s", key)
    return v


def doc_id(world, key):
    [(v,)] = q(world, "SELECT id::text FROM kh.documents WHERE doc_key = %s", key)
    return v


def alerts(world):
    return {m for (m,) in q(world, "SELECT message FROM kh.index_alerts")}


async def test_supersede_marks_the_old_document(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"old.md": md("old"), "new.md": md("new", supersedes=["old"])})
    await sync(indexer_pool, reader)
    assert sup(world) == doc_id(world, "new")
    assert sup(world, "new") is None


async def test_removing_supersedes_restores_the_old_document(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"old.md": md("old"), "new.md": md("new", supersedes=["old"])})
    await sync(indexer_pool, reader)
    fake.commit("kb-all", {"new.md": md("new")})
    await sync(indexer_pool, reader)
    assert sup(world) is None


async def test_deleting_the_successor_restores_the_old_document(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"old.md": md("old"), "new.md": md("new", supersedes=["old"])})
    await sync(indexer_pool, reader)
    fake.commit("kb-all", {"new.md": None})
    await sync(indexer_pool, reader)
    assert sup(world) is None


async def test_supersede_cycle_alerts_and_links_nothing(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("a", supersedes=["b"]), "b.md": md("b", supersedes=["a"])})
    await sync(indexer_pool, reader)
    assert sup(world, "a") is None and sup(world, "b") is None
    assert alerts(world) == {"supersedes cycle: a, b"}


async def test_unknown_supersedes_target_alerts(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"a.md": md("a", supersedes=["ghost"])})
    await sync(indexer_pool, reader)
    assert alerts(world) == {"supersedes unknown id ghost"}


async def test_two_successors_pick_the_smallest_doc_key(world, fake, reader, indexer_pool):
    fake.commit(
        "kb-all",
        {"old.md": md("old"), "z.md": md("z", supersedes=["old"]), "m.md": md("m", supersedes=["old"])},
    )
    await sync(indexer_pool, reader)
    assert sup(world) == doc_id(world, "m")
    assert alerts(world) == {"superseded by 2 documents; using m"}


async def test_unchanged_files_are_not_touched_by_a_second_sync(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"old.md": md("old"), "new.md": md("new", supersedes=["old"])})
    await sync(indexer_pool, reader)
    before = q(world, "SELECT doc_key, updated_at FROM kh.documents ORDER BY doc_key")
    fake.commit("kb-all", {"other.md": md("other")})
    await sync(indexer_pool, reader)
    after = q(world, "SELECT doc_key, updated_at FROM kh.documents WHERE doc_key <> 'other' ORDER BY doc_key")
    assert after == before


async def test_editing_a_superseded_document_keeps_it_superseded(world, fake, reader, indexer_pool):
    fake.commit("kb-all", {"old.md": md("old"), "new.md": md("new", supersedes=["old"])})
    await sync(indexer_pool, reader)
    fake.commit("kb-all", {"old.md": md("old", body="Changed body.\n")})
    await sync(indexer_pool, reader)
    assert sup(world) == doc_id(world, "new")
