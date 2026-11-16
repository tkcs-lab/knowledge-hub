"""Integration tests of the sync write (plan section 3.6) against PostgreSQL with the Forgejo fake."""

from __future__ import annotations

import psycopg

from knowledge_hub.config import parse_config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.sync import sync_repo
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake, approval
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


def offset_repo():
    """Anchor commit dated +01:00 (anchor-trusted a.md) and a PR approval with fractional seconds."""
    c, p, r = merged("m1", "a0", {"b.md": doc("b")}, 1)
    r[0]["submitted_at"] = "2026-11-02T11:00:00.750+01:00"
    repo = repo_of([(c, p, r)], {"a.md": doc("a")})
    repo.commits[0].date = "2026-11-01T11:00:00+01:00"
    return repo


SNAP = (
    "SELECT d.doc_key, d.id, d.indexed_at, d.updated_at, d.approved_at, c.id, c.content_sha"
    " FROM kh.documents d LEFT JOIN kh.chunks c ON c.document_id = d.id ORDER BY 1, 7"
)


async def test_offset_and_fractional_dates_resync_unchanged(fresh_db, indexer_pool):
    r = offset_repo()
    await sync(indexer_pool, r)
    first = rows(fresh_db, SNAP)
    state = rows(fresh_db, "SELECT * FROM kh.repo_state")
    stamps = {x[0]: x[4].isoformat() for x in first}
    assert stamps == {"a": "2026-11-01T10:00:00+00:00", "b": "2026-11-02T10:00:00+00:00"}
    await sync(indexer_pool, r)
    assert rows(fresh_db, SNAP) == first
    assert rows(fresh_db, "SELECT * FROM kh.repo_state") == state


UUID = "11111111-1111-1111-1111-111111111111"


def proposal_repo(approvers):
    commit = FakeCommit("m1", ["a0", "h1"], {"a.md": doc("a")}, "2026-11-02T10:00:00Z")
    pull = {
        "number": 1,
        "html_url": "https://forgejo.test/kh/repo/pulls/1",
        "merged": True,
        "merge_commit_sha": "m1",
        "user": {"login": "kh-hub-bot"},
        "base": {"ref": "main"},
        "head": {"sha": "h1", "ref": f"bot/propose/{UUID}"},
    }
    reviews = [approval("h1", who) for who in approvers]
    return FakeRepo([FakeCommit("a0", [], {}), commit], [pull], {1: reviews})


def requested_by(login):
    return {
        "context": "kh/requested-by",
        "state": "success",
        "description": f"{login} {UUID}",
        "creator": "kh-hub-bot",
    }


async def test_proposal_approved_only_by_requester_is_disputed(fresh_db, indexer_pool):
    fake = ForgejoFake({"canonical-all": proposal_repo(["bob"])})
    fake.statuses[("canonical-all", "h1")] = [requested_by("bob")]
    await sync_repo(indexer_pool, fake, CFG, "canonical-all", FakeEmbedder())
    assert rows(fresh_db, "SELECT status, approved_by FROM kh.documents") == [("disputed", [])]


async def test_proposal_approved_by_another_is_trusted(fresh_db, indexer_pool):
    fake = ForgejoFake({"canonical-all": proposal_repo(["bob", "carol"])})
    fake.statuses[("canonical-all", "h1")] = [requested_by("bob")]
    await sync_repo(indexer_pool, fake, CFG, "canonical-all", FakeEmbedder())
    assert rows(fresh_db, "SELECT status, approved_by FROM kh.documents") == [("current", ["carol"])]


async def test_proposal_without_record_is_disputed_in_sync(fresh_db, indexer_pool):
    fake = ForgejoFake({"canonical-all": proposal_repo(["carol"])})
    await sync_repo(indexer_pool, fake, CFG, "canonical-all", FakeEmbedder())
    assert rows(fresh_db, "SELECT status FROM kh.documents") == [("disputed",)]


NOTES_CFG = parse_config({"repos": {"notes-all": {"tier": "T1", "audience": "all"}}})


async def _sync_notes(pool, repo_obj):
    return await sync_repo(pool, ForgejoFake({"notes-all": repo_obj}), NOTES_CFG, "notes-all", FakeEmbedder())


async def test_agent_notes_are_flagged_and_a_squatted_id_keeps_the_human_note(fresh_db, indexer_pool):
    files = {
        "agent/n.md": doc("n", "type: agent-note\n"),
        "agent/m.md": doc("m"),  # type key removed: still flagged by path
        "h.md": doc("h"),
    }
    await _sync_notes(indexer_pool, FakeRepo([FakeCommit("n1", [], files)]))
    flags = {k: v for k, v in rows(fresh_db, "SELECT doc_key, agent_written FROM kh.documents")}
    assert flags == {"n": True, "m": True, "h": False}
    assert set(rows(fresh_db, "SELECT DISTINCT c.agent_written FROM kh.chunks c JOIN kh.documents d"
                              " ON d.id = c.document_id WHERE d.doc_key = 'n'")) == {(True,)}
    # An agent squats the id of the human note: the human note stays, the agent file is rejected.
    squat = dict(files, **{"agent/h.md": doc("h", "type: agent-note\n")})
    chain = [FakeCommit("n1", [], files), FakeCommit("n2", ["n1"], squat)]
    plan = await _sync_notes(indexer_pool, FakeRepo(chain))
    assert any(a.path == "agent/h.md" and "duplicate id h" in a.message for a in plan.alerts)
    assert rows(fresh_db, "SELECT path, agent_written FROM kh.documents WHERE doc_key = 'h'") == [
        ("h.md", False)
    ]
