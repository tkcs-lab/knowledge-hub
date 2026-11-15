"""Webhook endpoint and job worker (plan section 3.6) against PostgreSQL and the Forgejo fake."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import psycopg
import pytest

from knowledge_hub.app import build_app
from knowledge_hub.config import parse_config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.worker import Worker
from knowledge_hub.mcp.server import Deps
from tests.support.claims import verifier_for_tests
from tests.support.forgejo_fake import ForgejoFake
from tests.unit.test_sync_plan import doc, merged, repo_of

DB = "kh_fresh"
SECRET = "test-only-webhook-secret"
CFG = parse_config({"repos": {"canonical-all": {"tier": "T0", "audience": "all", "trust_anchor": "a0"}}})


def rows(fresh_db, sql, *args):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


@pytest.fixture
def secret_file(tmp_path):
    p = tmp_path / "hmac"
    p.write_text(SECRET + "\n")
    return p


@pytest.fixture
def client(db_settings, secret_file, app_pool, indexer_pool):
    settings = db_settings.model_copy(update={"env": "test", "webhook_hmac_file": secret_file})
    deps = Deps(settings=settings, config=CFG, app_pool=app_pool, indexer_pool=indexer_pool)
    app = build_app(settings, deps, verifier_for_tests())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub.test")


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def post(client, payload, event="push", secret=SECRET):
    body = json.dumps(payload).encode()
    headers = {"X-Forgejo-Event": event, "X-Forgejo-Signature": sign(body, secret)}
    return await client.post("/webhooks/forgejo", content=body, headers=headers)


PUSH = {"ref": "refs/heads/main", "repository": {"full_name": "kh/canonical-all"}}


async def test_signed_push_creates_job(client, fresh_db):
    r = await post(client, PUSH)
    assert r.status_code == 202
    assert rows(fresh_db, "SELECT kind, repo, state FROM kh.index_jobs") == [
        ("git_push", "canonical-all", "queued")
    ]


async def test_bad_signature_creates_nothing(client, fresh_db):
    r = await post(client, PUSH, secret="wrong")
    assert r.status_code == 401 and r.content == b""
    assert rows(fresh_db, "SELECT count(*) FROM kh.index_jobs") == [(0,)]


async def test_pr_event_payload_is_minimal(client, fresh_db):
    pr = {
        "action": "closed",
        "number": 7,
        "pull_request": {"title": "secret title"},
        "repository": {"full_name": "kh/canonical-all"},
    }
    assert (await post(client, pr, event="pull_request")).status_code == 202
    assert rows(fresh_db, "SELECT kind, payload FROM kh.index_jobs") == [
        ("pr_event", {"repo": "canonical-all", "action": "closed", "number": 7})
    ]


async def test_ignored_payloads_answer_202_without_job(client, fresh_db):
    other_org = {"ref": "refs/heads/main", "repository": {"full_name": "evil/canonical-all"}}
    unlisted = {"ref": "refs/heads/main", "repository": {"full_name": "kh/other"}}
    branch = {"ref": "refs/heads/dev", "repository": {"full_name": "kh/canonical-all"}}
    for p in (other_org, unlisted, branch):
        assert (await post(client, p)).status_code == 202
    assert rows(fresh_db, "SELECT count(*) FROM kh.index_jobs") == [(0,)]


def worker(pool, forgejo):
    return Worker(pool, forgejo, FakeEmbedder(), CFG)


def enqueue(fresh_db, kind="git_push"):
    rows(fresh_db, "INSERT INTO kh.index_jobs (kind, repo) VALUES (%s, 'canonical-all') RETURNING id", kind)


async def test_run_once_indexes_and_marks_done(fresh_db, indexer_pool):
    enqueue(fresh_db)
    forgejo = ForgejoFake({"canonical-all": repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1)])})
    w = worker(indexer_pool, forgejo)
    assert await w.run_once() is True
    assert rows(fresh_db, "SELECT state, attempts FROM kh.index_jobs") == [("done", 1)]
    assert rows(fresh_db, "SELECT doc_key FROM kh.documents") == [("a",)]
    assert await w.run_once() is False


async def test_failing_sync_is_failed_with_retry(fresh_db, indexer_pool):
    enqueue(fresh_db)
    w = worker(indexer_pool, ForgejoFake({}))  # unknown repo: the fake raises
    assert await w.run_once() is True
    ((state, attempts, err, delayed),) = rows(
        fresh_db,
        "SELECT state, attempts, last_error, run_after > now() + interval '20 seconds' FROM kh.index_jobs",
    )
    assert (state, attempts, delayed) == ("failed", 1, True) and err
    assert await w.run_once() is False  # not due yet


async def test_job_is_not_retried_after_five_attempts(fresh_db, indexer_pool):
    enqueue(fresh_db)
    w = worker(indexer_pool, ForgejoFake({}))
    for _ in range(5):
        assert await w.run_once() is True
        rows(fresh_db, "UPDATE kh.index_jobs SET run_after = now() RETURNING id")
    assert await w.run_once() is False
    assert rows(fresh_db, "SELECT state, attempts FROM kh.index_jobs") == [("failed", 5)]


async def test_pr_event_is_recorded_done(fresh_db, indexer_pool):
    enqueue(fresh_db, "pr_event")
    assert await worker(indexer_pool, ForgejoFake({})).run_once() is True
    assert rows(fresh_db, "SELECT state FROM kh.index_jobs") == [("done",)]
