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


async def sync(pool, repo_obj, **kw):
    return await sync_repo(
        pool, ForgejoFake({"canonical-all": repo_obj}), CFG, "canonical-all", FakeEmbedder(), **kw
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


# --- re-plan under the lock when the stored verdicts changed (plan section 3.6) ---


async def _count_plans(monkeypatch, loads):
    import knowledge_hub.indexer.sync as sync_mod

    real_load, calls, plans = sync_mod._load, iter(range(10)), []

    async def fake_load(conn, repo):
        state, existing, verdicts = await real_load(conn, repo)
        return state, existing, loads[next(calls)](verdicts)

    async def counting_plan(*args, **kwargs):
        plans.append(1)
        return await real_build(*args, **kwargs)

    real_build = sync_mod.build_sync_plan
    monkeypatch.setattr(sync_mod, "_load", fake_load)
    monkeypatch.setattr(sync_mod, "build_sync_plan", counting_plan)
    return plans


async def test_sync_replans_when_verdicts_change_under_the_lock(fresh_db, indexer_pool, monkeypatch):
    from knowledge_hub.indexer.trust import Verdict

    r = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1)])
    plans = await _count_plans(
        monkeypatch, [lambda v: v, lambda v: {**v, "zz": Verdict(status="disputed")}]
    )
    await sync(indexer_pool, r)
    assert len(plans) == 2


async def test_sync_plans_once_when_verdicts_are_equal(fresh_db, indexer_pool, monkeypatch):
    r = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1)])
    plans = await _count_plans(monkeypatch, [lambda v: v, lambda v: dict(v)])
    await sync(indexer_pool, r)
    assert len(plans) == 1


VERSION = "SELECT commit_sha, status, stamp, approved_by, approved_at FROM kh.document_versions"


async def test_plain_sync_of_trusted_merge_leaves_version_as_inserted(fresh_db, indexer_pool):
    r = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1)])
    await sync(indexer_pool, r)
    first = rows(fresh_db, VERSION)
    assert first[0][:4] == ("m1", "current", "reviewed", ["bob"])
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("UPDATE kh.document_versions SET approved_by = '{eve}'")
    changed = rows(fresh_db, VERSION)
    assert changed != first
    await sync(indexer_pool, r)
    assert rows(fresh_db, VERSION) == changed


async def test_plain_sync_in_untrusted_repo_leaves_version_unstamped(fresh_db, indexer_pool):
    c, p, rv = merged("m1", "a0", {"a.md": doc("a")}, 1)
    squash = FakeCommit("s1", ["m1"], {"b.md": doc("b")})
    r = FakeRepo([FakeCommit("a0", []), c, squash], [p], {1: rv})
    await sync(indexer_pool, r)
    assert rows(fresh_db, "SELECT trusted FROM kh.repo_state") == [(False,)]
    versions = rows(fresh_db, VERSION)
    assert versions and all(v[1:] == ("disputed", None, [], None) for v in versions)
    await sync(indexer_pool, r)
    assert rows(fresh_db, VERSION) == versions


async def test_rejudge_keeps_older_disputed_version_disputed(fresh_db, indexer_pool):
    m1 = merged("m1", "a0", {"a.md": doc("a")}, 1, approve=False)
    m2 = merged("m2", "m1", {"a.md": doc("a", title="T2")}, 2)
    await sync(indexer_pool, repo_of([m1]))
    await sync(indexer_pool, repo_of([m1, m2]))
    await sync(indexer_pool, repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1), m2]), rejudge=True)
    by_sha = {v[0]: v for v in rows(fresh_db, VERSION)}
    assert by_sha["m1"][1:] == ("disputed", None, [], None)
    assert by_sha["m2"][1:3] == ("current", "reviewed")


async def test_rejudge_with_per_file_dispute_at_head(fresh_db, indexer_pool):
    bad = doc("a", "supersedes: ghost\n")
    r = repo_of([merged("m1", "a0", {"a.md": bad}, 1)])
    await sync(indexer_pool, r)
    await sync(indexer_pool, r, rejudge=True)
    assert rows(fresh_db, VERSION)[0][1:] == ("disputed", None, [], None)
    assert rows(fresh_db, "SELECT status, stamp FROM kh.documents") == [("disputed", None)]
