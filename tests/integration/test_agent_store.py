"""agent/store.py and agent/guards.py against the database (plan 16 3.8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from knowledge_hub.agent import guards, store
from knowledge_hub.config import Config
from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.mcp.errors import KhError
from knowledge_hub.mcp.server import Deps
from tests.integration.test_derived_visibility_sql import doc as make_doc
from tests.integration.test_derived_visibility_sql import uid
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool):
    return Deps(settings=app_settings, config=Config(), app_pool=app_pool, indexer_pool=indexer_pool)


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


async def add(
    deps, caller, kind="doc", repo="canonical-all", domain="product", login="alice", key=None, **kw
):
    pid = uuid.uuid4()
    await store.insert_proposal(
        deps, caller, id=pid, kind=kind, repo=repo, path=kw.pop("path", f"{domain}/x.md"), doc_key=uid(),
        domain=domain, title="t", content_sha=kw.pop("content_sha", "sha"), requester_login=login,
        dedupe_key=None if kind == "note" else (key or uid()), **kw,
    )  # fmt: skip
    return pid


async def read(deps, caller, fn, *a, **kw):
    async with caller_tx(deps.app_pool, caller) as conn:
        return await fn(conn, *a, **kw)


def backdate(admin, pid, when):
    admin.execute("UPDATE kh.proposals SET created_at = %s WHERE id = %s", (when, pid))


async def test_insert_then_find_by_key(deps):
    key = uid()
    pid = await add(deps, alice_all, key=key, branch="bot/propose/x", base_sha="b1")
    found = await read(deps, alice_all, store.find_by_key, key)
    assert found.id == pid and found.state == "pending" and found.branch == "bot/propose/x"
    assert found.pr_number is None
    assert await read(deps, alice_all, store.find_by_key, uid()) is None
    assert await read(deps, bob_mgmt, store.find_by_key, key) is None  # another caller never sees it


async def test_insert_records_repo_audience(deps, admin):
    await add(deps, alice_all)
    assert admin.execute(
        "SELECT required_audiences, allowed_emails, created_by_sub FROM kh.proposals"
    ).fetchone() == (["all"], None, "alice")


async def test_mark_transitions(deps, admin):
    key = uid()
    pid = await add(deps, alice_all, key=key, branch="bot/propose/x")
    await store.mark(deps, alice_all, pid, "failed", error="boom")
    assert await read(deps, alice_all, store.find_by_key, key) is None  # failed is not open
    await store.mark(deps, alice_all, pid, "pr_open", commit_sha="c1", pr=(4, "http://pr/4"))
    found = await read(deps, alice_all, store.find_by_key, key)
    assert (found.state, found.commit_sha, found.pr_number, found.pr_url) == (
        "pr_open",
        "c1",
        4,
        "http://pr/4",
    )
    assert found.branch == "bot/propose/x"  # a null argument keeps the stored value
    with pytest.raises(KhError) as exc:  # pr_open is final for the caller
        await store.mark(deps, alice_all, pid, "failed")
    assert exc.value.code == "internal"


async def test_mark_refuses_another_caller_and_wrong_kind(deps):
    pid = await add(deps, alice_all)
    with pytest.raises(KhError):
        await store.mark(deps, bob_mgmt, pid, "pr_open", pr=(1, "u"))
    note = await add(deps, alice_all, kind="note", repo="notes-all", path="agent/n.md")
    with pytest.raises(KhError):
        await store.mark(deps, alice_all, note, "pr_open", pr=(1, "u"))
    await store.mark(deps, alice_all, note, "committed", commit_sha="c")


async def test_counts_include_other_callers_and_failed_rows(deps):
    a = await add(deps, alice_all, login="alice")
    await add(deps, bob_mgmt, login="bob", repo="canonical-mgmt")
    await add(deps, bob_mgmt, kind="edit", login="bob", domain="technical")
    await add(deps, alice_all, kind="note", repo="notes-all", path="agent/n.md", login="alice")
    await store.mark(deps, alice_all, a, "failed", error="x")
    kinds = ["doc", "edit"]
    count = store.count_today
    now = datetime.now(UTC)
    assert await read(deps, alice_all, count, kinds=kinds, now=now) == 3  # bob's rows and the failed one
    assert await read(deps, alice_all, count, kinds=kinds, now=now, login="bob") == 2
    assert await read(deps, alice_all, count, kinds=kinds, now=now, domain="technical") == 1
    assert await read(deps, alice_all, count, kinds=kinds, now=now, repo="canonical-mgmt") == 1
    assert await read(deps, alice_all, count, kinds=["note"], now=now, login="alice") == 1
    assert await read(deps, bob_mgmt, count, kinds=["note"], now=now) == 1  # no visibility filter


async def test_counts_reset_at_midnight_utc(deps, admin):
    old = await add(deps, alice_all)
    await add(deps, alice_all)
    now = datetime(2026, 11, 16, 0, 30, tzinfo=UTC)
    backdate(admin, old, datetime(2026, 11, 15, 23, 59, 59, tzinfo=UTC))
    # The other row was created at the real current time; fix both relative to the passed ``now``.
    admin.execute(
        "UPDATE kh.proposals SET created_at = %s WHERE id <> %s",
        (datetime(2026, 11, 16, 0, 1, tzinfo=UTC), old),
    )
    assert await read(deps, alice_all, store.count_today, kinds=["doc"], now=now) == 1
    later = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    assert await read(deps, alice_all, store.count_today, kinds=["doc"], now=later) == 0
    assert (
        await read(deps, alice_all, store.count_today, kinds=["doc"], now=later - timedelta(seconds=1)) == 1
    )


async def test_count_candidates_today(deps, admin):
    base = dict(
        domain="product",
        target_repo="canonical-all",
        target_path="product/a.md",
        title="t",
        body_sha="s",
        created_by_sub="alice",
        required_audiences=["all"],
        state="proposed",
    )
    for i, ts in enumerate([datetime(2026, 11, 16, 1, tzinfo=UTC), datetime(2026, 11, 15, 23, tzinfo=UTC)]):
        r = dict(base, id=uid(), dedupe_key=f"k{i}", branch=f"b{i}", created_at=ts)
        with admin.transaction():
            admin.execute(
                f"INSERT INTO kh.candidates ({', '.join(r)}) VALUES ({', '.join(['%s'] * len(r))})",
                list(r.values()),
            )
            admin.execute(
                "INSERT INTO kh.candidate_sources"
                " (candidate_id, source_kind, source_id, src_required_audiences)"
                " VALUES (%s, 'doc', %s, '{}')",
                (r["id"], make_doc(admin, ("all",))),
            )
    assert await read(deps, alice_all, store.count_candidates_today, "product", "canonical-all", NOW) == 1
    assert await read(deps, alice_all, store.count_candidates_today, "technical", "canonical-all", NOW) == 0


async def test_note_written_by(deps):
    path = "agent/n.md"
    pid = await add(deps, alice_all, kind="note", repo="notes-all", path=path)
    assert not await read(deps, alice_all, store.note_written_by, "notes-all", path, "alice")  # pending
    await store.mark(deps, alice_all, pid, "committed", commit_sha="c")
    assert await read(deps, alice_all, store.note_written_by, "notes-all", path, "alice")
    assert not await read(deps, alice_all, store.note_written_by, "notes-all", path, "bob")
    assert not await read(deps, alice_all, store.note_written_by, "notes-all", "agent/o.md", "alice")
    assert not await read(deps, alice_all, store.note_written_by, "notes-hr", path, "alice")
    other = await add(deps, alice_all, kind="note", repo="notes-all", path="agent/f.md")
    await store.mark(deps, alice_all, other, "failed", error="x")
    assert await read(deps, alice_all, store.note_written_by, "notes-all", "agent/f.md", "alice")


async def test_find_unsettled_note(deps):
    path = "agent/n.md"
    first = await add(deps, alice_all, kind="note", repo="notes-all", path=path, content_sha="s1")
    await store.mark(deps, alice_all, first, "failed", error="x")
    second = await add(deps, alice_all, kind="note", repo="notes-all", path=path, content_sha="s1")
    find = store.find_unsettled_note
    assert await read(deps, alice_all, find, "notes-all", path, "alice", "s1") == second  # newest
    assert await read(deps, alice_all, find, "notes-all", path, "alice", "s2") is None
    assert await read(deps, bob_mgmt, find, "notes-all", path, "bob", "s1") is None
    await store.mark(deps, alice_all, second, "committed", commit_sha="c")
    assert await read(deps, alice_all, find, "notes-all", path, "alice", "s1") == first


async def test_check_citations(deps, admin):
    config = Config()
    check = guards.check_citations
    await read(deps, alice_all, check, config, "canonical-all", "no ids here")
    unknown = f"doc:{uid()}"
    await read(deps, alice_all, check, config, "canonical-all", unknown)  # nothing resolves: no routing
    ok = f"doc:{make_doc(admin, ('all',))}"
    await read(deps, alice_all, check, config, "canonical-all", f"see {ok}")
    mgmt = f"doc:{make_doc(admin, ('mgmt',))}"
    await read(deps, bob_mgmt, check, config, "canonical-mgmt", mgmt)
    with pytest.raises(KhError) as exc:  # a mgmt source into the all repo would widen it
        await read(deps, bob_mgmt, check, config, "canonical-all", mgmt)
    assert exc.value.code == "wider"
    many = " ".join(f"doc:{uid()}" for _ in range(51))
    with pytest.raises(KhError) as exc:
        await read(deps, alice_all, check, config, "canonical-all", many)
    assert exc.value.code == "too_many_sources"
