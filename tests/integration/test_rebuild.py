"""``rebuild`` and ``check`` (plan section 3.6) against PostgreSQL with the Forgejo fake."""

from __future__ import annotations

from types import SimpleNamespace

import psycopg

from knowledge_hub.config import parse_config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.rebuild import check, config_hash, rebuild
from knowledge_hub.mcp.server import Deps
from tests.support.forgejo_fake import ForgejoFake
from tests.support.self_merge_cases import build_trust_world, load_cases, make_fake
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


# --- stored merge verdicts and --rejudge (plan sections 3.5 and 3.6) ---

CASE = {c["name"]: c for c in load_cases()}["owner-self-merge-in-own-domain"]
VERDICTS = "SELECT merge_sha, status, stamp, approved_by, pr_number FROM kh.merge_verdicts ORDER BY 1"
DOCS = "SELECT status, stamp, approved_by FROM kh.documents"


def self_merge_deps(indexer_pool, teams=None, case=CASE):
    repos, config = build_trust_world(case)
    deps = make_deps(indexer_pool, None, config)
    deps.forgejo = make_fake(case, repos, teams)
    return deps


def left_team():
    return {t: set(m) - {"alice"} for t, m in CASE["teams"].items()}


async def test_first_sync_stores_owner_merge_verdict(fresh_db, indexer_pool):
    await rebuild(self_merge_deps(indexer_pool))
    assert rows(fresh_db, VERDICTS) == [("m1", "trusted", "owner_merge", ["alice"], 1)]
    assert rows(fresh_db, DOCS) == [("current", "owner_merge", ["alice"])]


async def test_stored_verdict_survives_membership_change_and_plain_rebuild(fresh_db, indexer_pool):
    await rebuild(self_merge_deps(indexer_pool))
    stamped = rows(fresh_db, "SELECT merge_sha, judged_at FROM kh.merge_verdicts")
    deps = self_merge_deps(indexer_pool, left_team())
    await rebuild(deps)
    await rebuild(deps, "canonical-all")
    assert rows(fresh_db, VERDICTS) == [("m1", "trusted", "owner_merge", ["alice"], 1)]
    assert rows(fresh_db, DOCS) == [("current", "owner_merge", ["alice"])]
    assert rows(fresh_db, "SELECT merge_sha, judged_at FROM kh.merge_verdicts") == stamped
    assert "is_team_member" not in deps.forgejo.calls


async def test_rejudge_replaces_verdict_and_document(fresh_db, indexer_pool):
    await rebuild(self_merge_deps(indexer_pool))
    deps = self_merge_deps(indexer_pool, left_team())
    await rebuild(deps, rejudge=True)
    assert rows(fresh_db, DOCS) == [("disputed", None, [])]
    assert rows(fresh_db, VERDICTS) == [("m1", "disputed", None, [], 1)]
    again = rows(fresh_db, "SELECT merge_sha, judged_at FROM kh.merge_verdicts")
    await rebuild(deps)
    assert rows(fresh_db, DOCS) == [("disputed", None, [])]
    assert rows(fresh_db, "SELECT merge_sha, judged_at FROM kh.merge_verdicts") == again


async def test_rejudge_updates_the_stored_version(fresh_db, indexer_pool):
    await rebuild(self_merge_deps(indexer_pool))
    sql = (
        "SELECT status, stamp, approved_by, approved_at FROM kh.document_versions WHERE commit_sha = 'm1'"
    )
    assert rows(fresh_db, sql)[0][:3] == ("current", "owner_merge", ["alice"])
    await rebuild(self_merge_deps(indexer_pool, left_team()), rejudge=True)
    assert rows(fresh_db, sql) == [("disputed", None, [], None)]


async def test_rejudge_drops_rows_for_shas_off_the_chain(fresh_db, indexer_pool):
    await rebuild(self_merge_deps(indexer_pool))
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO kh.merge_verdicts (repo, merge_sha, status, stamp)"
            " VALUES ('canonical-all', 'gone', 'trusted', 'reviewed')"
        )
    deps = self_merge_deps(indexer_pool)
    await rebuild(deps)
    assert [r[0] for r in rows(fresh_db, VERDICTS)] == ["gone", "m1"]
    await rebuild(deps, rejudge=True)
    assert [r[0] for r in rows(fresh_db, VERDICTS)] == ["m1"]


async def test_plain_approved_pr_stores_reviewed(fresh_db, indexer_pool):
    await rebuild(make_deps(indexer_pool, two_docs()))
    assert rows(fresh_db, VERDICTS) == [("m1", "trusted", "reviewed", ["bob"], 1)]


async def test_disputed_merge_is_stored_without_stamp(fresh_db, indexer_pool):
    r = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1, approve=False)])
    await rebuild(make_deps(indexer_pool, r))
    assert rows(fresh_db, VERDICTS) == [("m1", "disputed", None, [], 1)]


async def test_document_row_without_stamp_gets_its_stamp(fresh_db, indexer_pool):
    deps = make_deps(indexer_pool, two_docs())
    await rebuild(deps)
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("UPDATE kh.documents SET stamp = NULL")
    await rebuild(deps)
    assert rows(fresh_db, "SELECT stamp, approved_by FROM kh.documents") == [("reviewed", ["bob"])] * 2
