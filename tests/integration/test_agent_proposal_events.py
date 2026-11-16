"""pr_event jobs for agent proposals: merged, closed, reopened, mismatch, adoption, replay (plan 3.8)."""

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


def set_pull(forgejo, number, head, *, merged=False, state="closed"):
    pulls = forgejo.repos[REPO].pulls
    pulls[:] = [p for p in pulls if p["number"] != number]
    pulls.append(
        {
            "number": number,
            "merged": merged,
            "state": state,
            "head": {"ref": head},
            "base": {"ref": "main"},
            "html_url": f"https://forgejo.test/kh/{REPO}/pulls/{number}",
        }
    )


def proposal(admin, *, state="pr_open", pr_number=7, repo=REPO, branch=True) -> str:
    pid = str(uuid.uuid4())
    with admin.transaction():
        admin.execute("SET LOCAL session_replication_role = replica")
        admin.execute(
            "INSERT INTO kh.proposals (id, kind, state, repo, path, doc_key, domain, title, content_sha,"
            " dedupe_key, branch, pr_number, requester_login, created_by_sub, required_audiences)"
            " VALUES (%s, 'doc', %s, %s, 'product/x.md', 'x', 'product', 't', 's', %s, %s, %s,"
            " 'alice', 'sub', '{all}')",
            (pid, state, repo, pid, f"bot/propose/{pid}" if branch else None, pr_number),
        )
    return pid


def enqueue(admin, number=7, action="closed"):
    admin.execute(
        "INSERT INTO kh.index_jobs (kind, repo, payload) VALUES ('pr_event', %s, %s::jsonb)",
        (REPO, f'{{"repo": "{REPO}", "action": "{action}", "number": {number}}}'),
    )


async def run(admin, indexer_pool, forgejo):
    w = Worker(indexer_pool, forgejo, FakeEmbedder(), Config())
    assert await w.run_once() is True
    assert admin.execute("SELECT state FROM kh.index_jobs ORDER BY id DESC LIMIT 1").fetchone() == ("done",)


def row(admin, pid):
    return admin.execute(
        "SELECT state, decided_at IS NOT NULL, pr_number, pr_url FROM kh.proposals WHERE id = %s", (pid,)
    ).fetchone()


def alerts(admin):
    return [r[0] for r in admin.execute("SELECT message FROM kh.index_alerts").fetchall()]


async def test_merged_pr_marks_proposal_merged(admin, indexer_pool, forgejo):
    pid = proposal(admin)
    set_pull(forgejo, 7, f"bot/propose/{pid}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert row(admin, pid)[:3] == ("merged", True, 7)


async def test_closed_unmerged_pr_marks_proposal_closed(admin, indexer_pool, forgejo):
    pid = proposal(admin)
    set_pull(forgejo, 7, f"bot/propose/{pid}")
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert row(admin, pid)[:2] == ("closed", True)


async def test_open_pr_changes_nothing(admin, indexer_pool, forgejo):
    pid = proposal(admin)
    set_pull(forgejo, 7, f"bot/propose/{pid}", state="open")
    enqueue(admin, action="opened")
    await run(admin, indexer_pool, forgejo)
    assert row(admin, pid)[:2] == ("pr_open", False)


async def test_reopened_then_merged(admin, indexer_pool, forgejo):
    pid = proposal(admin)
    set_pull(forgejo, 7, f"bot/propose/{pid}")
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert row(admin, pid)[0] == "closed"
    set_pull(forgejo, 7, f"bot/propose/{pid}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert row(admin, pid)[0] == "merged"


async def test_mismatch_alerts_and_changes_nothing(admin, indexer_pool, forgejo):
    pid = proposal(admin, pr_number=9)
    set_pull(forgejo, 7, f"bot/propose/{pid}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert row(admin, pid)[:3] == ("pr_open", False, 9)
    assert alerts(admin) == [f"pull request {REPO}#7 does not match proposal {pid}"]


async def test_row_without_pr_number_adopts_the_event(admin, indexer_pool, forgejo):
    pid = proposal(admin, state="pending", pr_number=None)
    set_pull(forgejo, 7, f"bot/propose/{pid}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert row(admin, pid) == ("merged", True, 7, f"https://forgejo.test/kh/{REPO}/pulls/7")
    assert alerts(admin) == []


async def test_row_without_pr_number_and_other_branch_is_a_mismatch(admin, indexer_pool, forgejo):
    pid = proposal(admin, state="pending", pr_number=None, branch=False)
    set_pull(forgejo, 7, f"bot/propose/{pid}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert row(admin, pid)[:3] == ("pending", False, None)
    assert len(alerts(admin)) == 1


async def test_replayed_event_changes_nothing(admin, indexer_pool, forgejo):
    pid = proposal(admin)
    set_pull(forgejo, 7, f"bot/propose/{pid}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    first = admin.execute("SELECT decided_at FROM kh.proposals WHERE id = %s", (pid,)).fetchone()
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert admin.execute("SELECT decided_at FROM kh.proposals WHERE id = %s", (pid,)).fetchone() == first
    # a later closed event never undoes a merge
    set_pull(forgejo, 7, f"bot/propose/{pid}")
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert row(admin, pid)[0] == "merged"
    assert alerts(admin) == []


async def test_unknown_proposal_is_ignored(admin, indexer_pool, forgejo):
    set_pull(forgejo, 7, f"bot/propose/{uuid.uuid4()}", merged=True)
    enqueue(admin)
    await run(admin, indexer_pool, forgejo)
    assert alerts(admin) == []
