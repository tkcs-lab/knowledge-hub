"""pr_event jobs: promotion, rejection and conflict closing (plan 3.8, PR events)."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from knowledge_hub.config import Config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.worker import Worker
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake
from tests.support.pg import FRESH_DB

REPO = "canonical-all"


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


@pytest.fixture
def forgejo():
    return ForgejoFake({REPO: FakeRepo(commits=[FakeCommit("c1")])})


def add_pull(forgejo, number, head, *, merged=False, state="closed"):
    forgejo.repos[REPO].pulls.append(
        {"number": number, "merged": merged, "state": state, "head": {"ref": head}, "base": {"ref": "main"}}
    )


def candidate(admin, sources, *, state="pr_open", pr_number=7, repo=REPO) -> str:
    cid = str(uuid.uuid4())
    with admin.transaction():
        admin.execute("SET LOCAL session_replication_role = replica")
        admin.execute(
            "INSERT INTO kh.candidates (id, state, domain, target_repo, target_path, title, body_sha,"
            " dedupe_key, created_by_sub, pr_number, required_audiences)"
            " VALUES (%s, %s, 'product', %s, 'product/x.md', 't', 's', %s, 'sub', %s, '{all}')",
            (cid, state, repo, cid, pr_number),
        )
        for kind, sid in sources:
            admin.execute(
                "INSERT INTO kh.candidate_sources"
                " (candidate_id, source_kind, source_id, src_required_audiences)"
                " VALUES (%s, %s, %s, '{all}')",
                (cid, kind, sid),
            )
    return cid


def conflict(admin, items) -> str:
    cid = str(uuid.uuid4())
    with admin.transaction():
        admin.execute("SET LOCAL session_replication_role = replica")
        admin.execute(
            "INSERT INTO kh.conflicts (id, state, created_by_sub, required_audiences)"
            " VALUES (%s, 'open', 'sub', '{all}')",
            (cid,),
        )
        for kind, iid in items:
            admin.execute(
                "INSERT INTO kh.conflict_items (conflict_id, item_kind, item_id, src_required_audiences)"
                " VALUES (%s, %s, %s, '{all}')",
                (cid, kind, iid),
            )
    return cid


def enqueue(admin, number=7, action="closed", repo=REPO):
    admin.execute(
        "INSERT INTO kh.index_jobs (kind, repo, payload) VALUES ('pr_event', %s, %s::jsonb)",
        (repo, f'{{"repo": "{repo}", "action": "{action}", "number": {number}}}'),
    )


async def run(admin, indexer_pool, forgejo):
    w = Worker(indexer_pool, forgejo, FakeEmbedder(), Config())
    assert await w.run_once() is True
    assert admin.execute("SELECT state FROM kh.index_jobs ORDER BY id DESC LIMIT 1").fetchone() == ("done",)


def cand_state(admin, cid):
    return admin.execute(
        "SELECT state, decided_at IS NOT NULL FROM kh.candidates WHERE id = %s", (cid,)
    ).fetchone()


def conflict_row(admin, cid):
    return admin.execute(
        "SELECT state, closed_by_pr, closed_at IS NOT NULL FROM kh.conflicts WHERE id = %s", (cid,)
    ).fetchone()


async def test_merged_pr_promotes_and_closes_related_conflicts_only(admin, indexer_pool, forgejo):
    src, other = str(uuid.uuid4()), str(uuid.uuid4())
    cid = candidate(admin, [("doc", src)])
    related = conflict(admin, [("doc", src), ("doc", other)])
    unrelated = conflict(admin, [("doc", other)])
    same_id_other_kind = conflict(admin, [("raw", src)])
    add_pull(forgejo, 7, f"bot/promote/{cid}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert cand_state(admin, cid) == ("promoted", True)
    assert conflict_row(admin, related) == ("closed", f"{REPO}#7", True)
    assert conflict_row(admin, unrelated) == ("open", None, False)
    assert conflict_row(admin, same_id_other_kind) == ("open", None, False)


async def test_closed_unmerged_pr_rejects_and_keeps_conflicts(admin, indexer_pool, forgejo):
    src = str(uuid.uuid4())
    cid = candidate(admin, [("doc", src)])
    c = conflict(admin, [("doc", src)])
    add_pull(forgejo, 7, f"bot/promote/{cid}", merged=False)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert cand_state(admin, cid) == ("rejected", True)
    assert conflict_row(admin, c)[0] == "open"


async def test_open_pr_changes_nothing(admin, indexer_pool, forgejo):
    cid = candidate(admin, [("doc", str(uuid.uuid4()))])
    add_pull(forgejo, 7, f"bot/promote/{cid}", state="open")
    enqueue(admin, action="opened")
    await run(admin, indexer_pool, forgejo)
    assert cand_state(admin, cid) == ("pr_open", False)


async def test_pr_from_another_branch_is_ignored(admin, indexer_pool, forgejo):
    src = str(uuid.uuid4())
    cid = candidate(admin, [("doc", src)])
    c = conflict(admin, [("doc", src)])
    add_pull(forgejo, 7, f"bot/promoteX{cid}", merged=True)  # same length as the prefix, different text
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert cand_state(admin, cid) == ("pr_open", False)
    assert conflict_row(admin, c)[0] == "open"
    assert admin.execute("SELECT count(*) FROM kh.index_alerts").fetchone() == (0,)


async def test_pr_number_mismatch_is_ignored_with_alert(admin, indexer_pool, forgejo):
    src = str(uuid.uuid4())
    cid = candidate(admin, [("doc", src)], pr_number=7)
    c = conflict(admin, [("doc", src)])
    add_pull(forgejo, 8, f"bot/promote/{cid}", merged=True)
    enqueue(admin, number=8)
    await run(admin, indexer_pool, forgejo)
    assert cand_state(admin, cid) == ("pr_open", False)
    assert conflict_row(admin, c)[0] == "open"
    assert admin.execute("SELECT repo, message FROM kh.index_alerts").fetchall() == [
        (REPO, f"pull request {REPO}#8 does not match candidate {cid}")
    ]
    enqueue(admin, number=8)
    await run(admin, indexer_pool, forgejo)
    assert admin.execute("SELECT count(*) FROM kh.index_alerts").fetchone() == (1,)  # deduped


async def test_repo_mismatch_is_ignored_with_alert(admin, indexer_pool, forgejo):
    cid = candidate(admin, [("doc", str(uuid.uuid4()))], repo="canonical-mgmt")
    add_pull(forgejo, 7, f"bot/promote/{cid}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert cand_state(admin, cid) == ("pr_open", False)
    assert admin.execute("SELECT count(*) FROM kh.index_alerts").fetchone() == (1,)


async def test_same_event_twice_is_a_no_op(admin, indexer_pool, forgejo):
    src = str(uuid.uuid4())
    cid = candidate(admin, [("doc", src)])
    c = conflict(admin, [("doc", src)])
    add_pull(forgejo, 7, f"bot/promote/{cid}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    before = (
        admin.execute("SELECT state, decided_at FROM kh.candidates WHERE id = %s", (cid,)).fetchone(),
        admin.execute(
            "SELECT state, closed_at, closed_by_pr FROM kh.conflicts WHERE id = %s", (c,)
        ).fetchone(),
    )
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    after = (
        admin.execute("SELECT state, decided_at FROM kh.candidates WHERE id = %s", (cid,)).fetchone(),
        admin.execute(
            "SELECT state, closed_at, closed_by_pr FROM kh.conflicts WHERE id = %s", (c,)
        ).fetchone(),
    )
    assert before == after


async def test_rejected_candidate_stays_rejected_on_replay(admin, indexer_pool, forgejo):
    cid = candidate(admin, [("doc", str(uuid.uuid4()))])
    add_pull(forgejo, 7, f"bot/promote/{cid}")
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    first = admin.execute("SELECT decided_at FROM kh.candidates WHERE id = %s", (cid,)).fetchone()
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert admin.execute("SELECT decided_at FROM kh.candidates WHERE id = %s", (cid,)).fetchone() == first


async def test_a_promoted_candidate_is_not_rejected_by_a_stale_event(admin, indexer_pool, forgejo):
    cid = candidate(admin, [("doc", str(uuid.uuid4()))], state="promoted")
    add_pull(forgejo, 7, f"bot/promote/{cid}", merged=False)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert cand_state(admin, cid)[0] == "promoted"


async def test_unknown_candidate_is_recorded_done(admin, indexer_pool, forgejo):
    add_pull(forgejo, 7, f"bot/promote/{uuid.uuid4()}", merged=True)
    add_pull(forgejo, 9, "bot/promote/not-a-uuid", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    enqueue(admin, number=9)
    await run(admin, indexer_pool, forgejo)
    assert admin.execute("SELECT count(*) FROM kh.index_alerts").fetchone() == (0,)


async def test_merged_pr_for_a_candidate_that_never_opened_a_pr_changes_nothing(admin, indexer_pool, forgejo):
    src = str(uuid.uuid4())
    cid = candidate(admin, [("doc", src)], state="failed")
    c = conflict(admin, [("doc", src)])
    add_pull(forgejo, 7, f"bot/promote/{cid}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert cand_state(admin, cid) == ("failed", False)
    assert conflict_row(admin, c)[0] == "open"
