"""Archived groups are skipped by the webhook, the worker, sync and rebuild."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import psycopg
import pytest

from knowledge_hub.app import build_app
from knowledge_hub.config import GroupConfig
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.rebuild import configured_repos, rebuild
from knowledge_hub.indexer.sync import sync_repo
from knowledge_hub.indexer.worker import Worker
from knowledge_hub.mcp.server import Deps
from knowledge_hub.metrics import METRICS
from tests.support.blobs import MemoryBlobStore
from tests.support.claims import verifier_for_tests
from tests.support.principals import KH_CONFIG

DB = "kh_fresh"
SECRET = "hook-secret"


@pytest.fixture(autouse=True)
def fresh_metrics():
    METRICS.reset()
    yield
    METRICS.reset()


ARCHIVED = KH_CONFIG.model_copy(
    update={
        "groups": {
            **KH_CONFIG.groups,
            "hr": GroupConfig(repo="kb-hr", members=["carol"], approvers=["carol"], archived=True),
        }
    }
)


@pytest.fixture
def kh_config():
    return ARCHIVED


@pytest.fixture
def world(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        for name in KH_CONFIG.groups:
            conn.execute(
                "INSERT INTO kh.groups (name, repo) VALUES (%s, %s)", (name, KH_CONFIG.repo_for_group(name))
            )
    return fresh_db


def q(world, sql, *args):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        return conn.execute(sql, args).fetchall()


@pytest.fixture
def hook_settings(app_settings, tmp_path):
    secret = tmp_path / "hmac"
    secret.write_text(SECRET + "\n")
    return app_settings.model_copy(update={"webhook_hmac_file": secret, "max_body_bytes": 4096})


@pytest.fixture
def deps(hook_settings, kh_config, app_pool, indexer_pool):
    return Deps(settings=hook_settings, config=kh_config, app_pool=app_pool, indexer_pool=indexer_pool)


@pytest.fixture
async def http(hook_settings, deps):
    app = build_app(hook_settings, deps, verifier_for_tests())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as client:
        yield client


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def post(http, payload, *, event="push", secret=SECRET, raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"x-forgejo-event": event, "x-forgejo-signature": sign(body, secret)}
    return await http.post("/webhooks/forgejo", content=body, headers=headers)


def push(full_name="kh/kb-all", ref="refs/heads/main"):
    return {"ref": ref, "repository": {"full_name": full_name}}


def jobs(world):
    return q(world, "SELECT kind, repo, state FROM kh.index_jobs ORDER BY id")


# --- tests -----------------------------------------------------------------------------------


class NoReads:
    def __getattr__(self, name):
        raise AssertionError(f"reader.{name} must not be called for an archived repo")


async def test_push_to_archived_repo_is_ignored(world, http):
    r = await post(http, push("kh/kb-hr"))
    assert r.status_code == 202
    assert jobs(world) == []


async def test_worker_finishes_archived_git_push_job_without_syncing(world, indexer_pool):
    with psycopg.connect(world.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.index_jobs (kind, repo, payload) VALUES ('git_push', 'kb-hr', '{}')")
    embedder = FakeEmbedder(1024)
    worker = Worker(indexer_pool, NoReads(), embedder, ARCHIVED, MemoryBlobStore(), chunker_version=1)
    assert await worker.run_once() is True
    assert q(world, "SELECT state FROM kh.index_jobs") == [("done",)]


async def test_sync_repo_refuses_archived_repo(world, indexer_pool):
    with pytest.raises(ValueError, match="archived"):
        await sync_repo(indexer_pool, NoReads(), ARCHIVED, "kb-hr", FakeEmbedder(1024), chunker_version=1)


async def test_rebuild_skips_archived_and_refuses_it_by_name(world, hook_settings, indexer_pool, app_pool):
    deps = Deps(settings=hook_settings, config=ARCHIVED, app_pool=app_pool, indexer_pool=indexer_pool)
    assert "kb-hr" not in configured_repos(ARCHIVED)
    with pytest.raises(ValueError, match="archived"):
        await rebuild(deps, "kb-hr")
