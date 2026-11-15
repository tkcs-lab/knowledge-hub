"""``kh recompute-visibility`` and ``kh requeue-embeddings`` (plan 3.7) against PostgreSQL."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import psycopg
import pytest

from knowledge_hub.config import Config, parse_config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.rebuild import check, config_hash
from knowledge_hub.jobs.audience_map import load_audience_map
from knowledge_hub.jobs.recompute import recompute_visibility
from knowledge_hub.jobs.requeue import requeue_embeddings
from knowledge_hub.mcp.server import Deps
from tests.integration.test_derived_visibility_sql import derived_row, doc, insert, raw, source
from tests.integration.test_strict_visibility import (
    candidate_row,
    candidate_source,
    conflict_item,
    conflict_row,
)
from tests.support.forgejo_fake import ForgejoFake
from tests.unit.test_sync_plan import doc as doc_file
from tests.unit.test_sync_plan import merged, repo_of

DB = "kh_fresh"
ALL = Config(raw_audience_map={"series:s1": ["all"]})
MGMT = Config(raw_audience_map={"series:s1": ["mgmt"]})


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        yield conn


def uid() -> str:
    return str(uuid.uuid4())


def mapped_raw(admin, series="s1", aud=("all",)) -> str:
    rid = raw(admin, aud)
    admin.execute("UPDATE kh.raw_records SET series = %s WHERE id = %s", (series, rid))
    return rid


def derived_from(admin, aud, emails, *cited) -> str:
    did = uid()
    with admin.transaction():
        derived_row(admin, aud, emails, did=did)
        for kind, sid in cited:
            source(admin, did, kind, sid)
    return did


def candidate_from(admin, aud, emails, *cited) -> str:
    cid = uid()
    with admin.transaction():
        candidate_row(admin, aud, emails, cid=cid)
        for kind, sid in cited:
            candidate_source(admin, cid, kind, sid)
    return cid


def conflict_from(admin, aud, emails, *cited) -> str:
    cid = uid()
    with admin.transaction():
        conflict_row(admin, aud, emails, cid=cid)
        for kind, sid in cited:
            conflict_item(admin, cid, kind, sid)
    return cid


def chunk(admin, column: str, owner: str) -> None:
    insert(
        admin,
        "chunks",
        {
            "id": uid(),
            column: owner,
            "ordinal": 0,
            "text": "t",
            "content_sha": "s",
            "owner_key": "",
            "tier": "T3",
            "required_audiences": [],
        },
    )


def vis(admin, table: str, rid: str):
    return admin.execute(
        f"SELECT required_audiences, allowed_emails FROM kh.{table} WHERE id = %s", (rid,)
    ).fetchone()


def snapshot(admin):
    parts = []
    for table in ("documents", "raw_records", "derived_records", "candidates", "conflicts", "chunks"):
        parts.append(
            admin.execute(
                f"SELECT id, required_audiences, allowed_emails, xmin::text FROM kh.{table} ORDER BY id"
            ).fetchall()
        )
    for table, key in (
        ("derived_sources", "derived_id"),
        ("candidate_sources", "candidate_id"),
        ("conflict_items", "conflict_id"),
    ):
        parts.append(
            admin.execute(
                f"SELECT {key}, src_required_audiences, src_allowed_emails, xmin::text"
                f" FROM kh.{table} ORDER BY 1, 2, 3"
            ).fetchall()
        )
    return parts


def build(admin):
    """A raw row in series s1, a chunk, derived rows (one citing the other), a candidate, a conflict."""
    r = mapped_raw(admin)
    chunk(admin, "raw_id", r)
    d1 = derived_from(admin, ("all",), None, ("raw", r))
    d2 = derived_from(admin, ("all",), None, ("derived", d1))
    c = candidate_from(admin, ("all",), None, ("raw", r))
    k = conflict_from(admin, ("all",), None, ("raw", r))
    return r, d1, d2, c, k


async def test_tightening_the_map_moves_everything_that_cites_the_row(admin, indexer_pool):
    await recompute_visibility(indexer_pool, ALL)
    r, d1, d2, c, k = build(admin)
    await load_audience_map(indexer_pool, MGMT)
    mgmt = (["mgmt"], None)
    assert vis(admin, "raw_records", r) == mgmt
    assert admin.execute("SELECT required_audiences, allowed_emails FROM kh.chunks").fetchall() == [mgmt]
    assert vis(admin, "derived_records", d1) == mgmt
    assert vis(admin, "derived_records", d2) == mgmt
    assert vis(admin, "candidates", c) == mgmt
    assert vis(admin, "conflicts", k) == mgmt
    snaps = admin.execute(
        "SELECT src_required_audiences FROM kh.derived_sources UNION ALL"
        " SELECT src_required_audiences FROM kh.candidate_sources UNION ALL"
        " SELECT src_required_audiences FROM kh.conflict_items"
    ).fetchall()
    assert snaps == [(["mgmt"],)] * 4
    assert admin.execute("SELECT config_hash FROM kh.repo_state WHERE repo = '__config__'").fetchone() == (
        config_hash(MGMT),
    )


async def test_loader_without_a_stored_hash_loads_the_map_only(admin, indexer_pool):
    r = mapped_raw(admin)
    admin.execute("UPDATE kh.raw_records SET required_audiences = '{hr}' WHERE id = %s", (r,))
    await load_audience_map(indexer_pool, ALL)
    assert vis(admin, "raw_records", r) == (["hr"], None)
    assert admin.execute("SELECT count(*) FROM kh.repo_state WHERE repo = '__config__'").fetchone() == (0,)
    assert admin.execute("SELECT key FROM kh.raw_audience_map").fetchall() == [("s1",)]


async def test_a_loader_run_with_an_unchanged_hash_does_not_recompute(admin, indexer_pool):
    await recompute_visibility(indexer_pool, ALL)
    r = mapped_raw(admin)
    admin.execute("UPDATE kh.raw_records SET required_audiences = '{hr}' WHERE id = %s", (r,))
    await load_audience_map(indexer_pool, ALL)
    assert vis(admin, "raw_records", r) == (["hr"], None)
    await recompute_visibility(indexer_pool, ALL)
    assert vis(admin, "raw_records", r) == (["all"], None)


async def test_recompute_reads_the_map_from_config(admin, indexer_pool):
    admin.execute("INSERT INTO kh.raw_audience_map VALUES ('series', 's1', '{all}')")
    r = mapped_raw(admin)
    await recompute_visibility(indexer_pool, MGMT)
    assert admin.execute("SELECT kind, key, audiences FROM kh.raw_audience_map").fetchall() == [
        ("series", "s1", ["mgmt"])
    ]
    assert vis(admin, "raw_records", r) == (["mgmt"], None)


async def test_unmapped_raw_keeps_participants_and_a_dropped_mapping_falls_back_to_them(admin, indexer_pool):
    unmapped = raw(admin, (), ["x@example.org"], participants=("X@Example.org", "y@example.org"))
    await recompute_visibility(indexer_pool, ALL)
    assert vis(admin, "raw_records", unmapped) == ([], ["x@example.org", "y@example.org"])
    r = mapped_raw(admin)
    admin.execute("UPDATE kh.raw_records SET participants = '{Z@example.org}' WHERE id = %s", (r,))
    await recompute_visibility(indexer_pool, ALL)
    assert vis(admin, "raw_records", r) == (["all"], None)
    await recompute_visibility(indexer_pool, Config())
    assert vis(admin, "raw_records", r) == ([], ["z@example.org"])


async def test_narrowing_a_repo_audience_narrows_documents_and_their_derived_rows(admin, indexer_pool):
    wide = parse_config(
        {
            "repos": {"canonical-all": {"tier": "T0", "audience": "all", "trust_anchor": "a0"}},
            "raw_audience_map": {"series:s1": ["all"]},
        }
    )
    narrow = parse_config(
        {
            "repos": {"canonical-all": {"tier": "T0", "audience": "mgmt", "trust_anchor": "a0"}},
            "raw_audience_map": {"series:s1": ["all"]},
        }
    )
    d = doc(admin, ("all",))
    other = insert(
        admin,
        "documents",
        dict(
            id=uid(),
            repo="elsewhere",
            path="p.md",
            doc_key=uid(),
            tier="T1",
            domain="product",
            title="t",
            body="b",
            status="current",
            commit_sha="c",
            content_sha="s",
            required_audiences=["hr"],
        ),
    )
    r = mapped_raw(admin)
    derived = derived_from(admin, ("all",), None, ("doc", d), ("raw", r))
    cand = candidate_from(admin, ("all",), None, ("doc", d))
    chunk(admin, "document_id", d)
    await recompute_visibility(indexer_pool, wide)
    assert vis(admin, "documents", d) == (["all"], None)
    await recompute_visibility(indexer_pool, narrow)
    assert vis(admin, "documents", d) == (["mgmt"], None)
    assert vis(admin, "documents", other) == (["hr"], None)  # a repo outside the config is left alone
    assert vis(admin, "derived_records", derived) == (["all", "mgmt"], None)  # the raw row stays {all}
    assert vis(admin, "candidates", cand) == (["mgmt"], None)
    assert admin.execute("SELECT required_audiences FROM kh.chunks").fetchall() == [(["mgmt"],)]


async def test_deleting_a_cited_document_never_widens(admin, indexer_pool):
    cfg = parse_config({"repos": {"canonical-all": {"tier": "T0", "audience": "mgmt", "trust_anchor": "a0"}}})
    d = doc(admin, ("mgmt",))
    derived = derived_from(admin, ("mgmt",), None, ("doc", d))
    d2 = derived_from(admin, ("mgmt",), None, ("derived", derived))
    cand = candidate_from(admin, ("mgmt",), None, ("doc", d))
    conf = conflict_from(admin, ("mgmt",), None, ("doc", d))
    admin.execute("DELETE FROM kh.documents WHERE id = %s", (d,))
    await recompute_visibility(indexer_pool, cfg)
    for table, rid in (
        ("derived_records", derived),
        ("derived_records", d2),
        ("candidates", cand),
        ("conflicts", conf),
    ):
        assert vis(admin, table, rid) == (["mgmt"], None), table
    await recompute_visibility(indexer_pool, Config())
    assert vis(admin, "derived_records", derived) == (["mgmt"], None)


async def test_email_lists_are_intersected_and_a_live_source_beats_its_snapshot(admin, indexer_pool):
    a = raw(admin, (), ["a@example.org", "b@example.org"], participants=("a@example.org", "b@example.org"))
    b = raw(admin, (), ["b@example.org", "c@example.org"], participants=("b@example.org", "c@example.org"))
    both = derived_from(admin, (), ["b@example.org"], ("raw", a), ("raw", b))
    await recompute_visibility(indexer_pool, ALL)
    assert vis(admin, "derived_records", both) == ([], ["b@example.org"])
    admin.execute("UPDATE kh.raw_records SET participants = '{c@example.org}' WHERE id = %s", (b,))
    await recompute_visibility(indexer_pool, ALL)
    assert vis(admin, "derived_records", both) == ([], [])  # empty intersection, not NULL
    mapped = mapped_raw(admin, aud=("hr",))
    await recompute_visibility(indexer_pool, ALL)  # the mapped row is {all}
    mixed = derived_from(admin, ("all",), ["a@example.org", "b@example.org"], ("raw", a), ("raw", mapped))
    await recompute_visibility(indexer_pool, ALL)
    assert vis(admin, "derived_records", mixed) == (["all"], ["a@example.org", "b@example.org"])


async def test_running_twice_changes_nothing(admin, indexer_pool):
    await recompute_visibility(indexer_pool, ALL)
    build(admin)
    d = doc(admin, ("all",))
    derived_from(admin, ("all",), None, ("doc", d))
    await recompute_visibility(indexer_pool, MGMT)
    first = snapshot(admin)
    await recompute_visibility(indexer_pool, MGMT)
    assert snapshot(admin) == first


async def test_a_citation_cycle_is_refused_and_rolls_back(admin, indexer_pool):
    d1 = derived_from(admin, ("all",), None, ("doc", doc(admin)))
    d2 = derived_from(admin, ("all",), None, ("derived", d1))
    with admin.transaction():  # superuser rewrites a citation to close the loop
        admin.execute("SET LOCAL session_replication_role = replica")
        admin.execute(
            "UPDATE kh.derived_sources SET source_kind = 'derived', source_id = %s WHERE derived_id = %s",
            (d2, d1),
        )
    with pytest.raises(ValueError, match="cycle"):
        await recompute_visibility(indexer_pool, MGMT)
    assert admin.execute("SELECT count(*) FROM kh.repo_state WHERE repo = '__config__'").fetchone() == (0,)


async def test_check_reports_a_stale_config_hash_before_and_clean_after(fresh_db, admin, indexer_pool):
    cfg = parse_config(
        {
            "repos": {"canonical-all": {"tier": "T0", "audience": "all", "trust_anchor": "a0"}},
            "raw_audience_map": {"series:s1": ["all"]},
        }
    )
    forgejo = ForgejoFake({"canonical-all": repo_of([merged("m1", "a0", {"a.md": doc_file("a")}, 1)])})
    deps = Deps(
        settings=SimpleNamespace(chunker_version=1),
        config=cfg,
        app_pool=None,
        indexer_pool=indexer_pool,
        embedder=FakeEmbedder(),
        forgejo=forgejo,
    )
    assert (await check(deps)).config_drift is True
    await recompute_visibility(indexer_pool, cfg)
    assert (await check(deps)).config_drift is False
    await recompute_visibility(indexer_pool, Config(raw_audience_map={"series:s1": ["mgmt"]}))
    assert (await check(deps)).config_drift is True


async def test_requeue_queues_only_records_without_chunks(admin, indexer_pool):
    r_with, r_without = mapped_raw(admin), mapped_raw(admin)
    d_with = derived_from(admin, ("all",), None, ("raw", r_with))
    d_without = derived_from(admin, ("all",), None, ("raw", r_with))
    chunk(admin, "raw_id", r_with)
    chunk(admin, "derived_id", d_with)
    admin.execute("DELETE FROM kh.index_jobs")
    assert await requeue_embeddings(indexer_pool) == 2
    jobs = admin.execute("SELECT kind, payload ->> 'id', state FROM kh.index_jobs ORDER BY kind").fetchall()
    assert jobs == [("embed_derived", d_without, "queued"), ("embed_raw", r_without, "queued")]
    assert await requeue_embeddings(indexer_pool) == 0  # already waiting
    admin.execute("UPDATE kh.index_jobs SET state = 'done'")
    assert await requeue_embeddings(indexer_pool) == 2  # done jobs do not count as waiting
