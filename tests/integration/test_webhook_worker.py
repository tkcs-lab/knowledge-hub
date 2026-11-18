"""The Forgejo webhook queues ``git_push`` jobs; the worker runs ``git_push`` and ``embed_record`` jobs."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import replace

import httpx
import psycopg
import pytest

from knowledge_hub.app import build_app
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.forgejo import ForgejoReader
from knowledge_hub.indexer import worker as worker_mod
from knowledge_hub.indexer.worker import MAX_ATTEMPTS, Worker
from knowledge_hub.mcp.server import Deps
from knowledge_hub.metrics import METRICS
from tests.support.blobs import MemoryBlobStore
from tests.support.claims import verifier_for_tests
from tests.support.forgejo_fake import FakeForgejo, md
from tests.support.principals import KH_CONFIG

DB = "kh_fresh"
SECRET = "hook-secret"


@pytest.fixture(autouse=True)
def fresh_metrics():
    METRICS.reset()
    yield
    METRICS.reset()


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


# --- webhook ---------------------------------------------------------------------------------


async def test_push_to_main_queues_a_git_push_job(world, http):
    r = await post(http, push())
    assert r.status_code == 202
    assert jobs(world) == [("git_push", "kb-all", "queued")]
    assert 'kh_webhook_events_total{result="queued"} 1' in METRICS.render()


async def test_bad_signature_is_refused(world, http):
    assert (await post(http, push(), secret="wrong")).status_code == 401
    assert jobs(world) == []


async def test_missing_signature_header_is_refused(world, http):
    r = await http.post("/webhooks/forgejo", content=b"{}", headers={"x-forgejo-event": "push"})
    assert r.status_code == 401


async def test_no_secret_file_refuses_everything(world, hook_settings, deps):
    settings = hook_settings.model_copy(update={"webhook_hmac_file": None})
    deps = replace(deps, settings=settings)
    app = build_app(settings, deps, verifier_for_tests())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as c:
        assert (await post(c, push())).status_code == 401


async def test_oversized_body_is_413(world, http):
    r = await post(http, None, raw=b"x" * 5000)
    assert r.status_code == 413
    assert jobs(world) == []


async def test_invalid_json_is_400(world, http):
    assert (await post(http, None, raw=b"not json")).status_code == 400
    assert (await post(http, None, raw=b"[1]")).status_code == 400


async def test_another_org_is_ignored(world, http):
    assert (await post(http, push("other/kb-all"))).status_code == 202
    assert jobs(world) == []


async def test_repo_outside_every_group_is_ignored(world, http):
    assert (await post(http, push("kh/not-configured"))).status_code == 202
    assert jobs(world) == []
    assert 'kh_webhook_events_total{result="ignored"} 1' in METRICS.render()


async def test_other_branch_is_ignored(world, http):
    assert (await post(http, push(ref="refs/heads/feature"))).status_code == 202
    assert jobs(world) == []


async def test_other_event_is_ignored(world, http):
    assert (await post(http, push(), event="pull_request")).status_code == 202
    assert jobs(world) == []


async def test_full_name_fallback_uses_owner_login_and_name(world, http):
    payload = {"ref": "refs/heads/main", "repository": {"name": "kb-mgmt", "owner": {"login": "kh"}}}
    assert (await post(http, payload)).status_code == 202
    assert jobs(world) == [("git_push", "kb-mgmt", "queued")]


async def test_fallback_with_another_owner_is_ignored(world, http):
    payload = {"ref": "refs/heads/main", "repository": {"name": "kb-mgmt", "owner": {"login": "other"}}}
    assert (await post(http, payload)).status_code == 202
    assert jobs(world) == []


async def test_payload_without_repository_is_ignored(world, http):
    assert (await post(http, {"ref": "refs/heads/main"})).status_code == 202
    assert jobs(world) == []


# --- worker ----------------------------------------------------------------------------------


@pytest.fixture
def fake():
    f = FakeForgejo()
    for name in ("kb-all", "kb-mgmt", "kb-hr"):
        f.add_repo(name)
    return f


@pytest.fixture
async def reader(fake):
    r = ForgejoReader("https://forgejo.test", "tok", "kh", transport=fake.transport)
    yield r
    await r.aclose()


@pytest.fixture
def blobs():
    return MemoryBlobStore()


@pytest.fixture
def make_worker(indexer_pool, reader, blobs):
    def make(**kwargs):
        args = dict(chunker_version=1, poll_seconds=0.05)
        args.update(kwargs)
        return Worker(indexer_pool, reader, FakeEmbedder(1024), KH_CONFIG, blobs, **args)

    return make


def add_job(world, kind, repo=None, payload="{}"):
    return q(
        world,
        "INSERT INTO kh.index_jobs (kind, repo, payload) VALUES (%s, %s, %s::jsonb) RETURNING id",
        kind,
        repo,
        payload,
    )[0][0]


def job(world, job_id):
    return q(world, "SELECT state, attempts, last_error FROM kh.index_jobs WHERE id = %s", job_id)[0]


async def test_run_once_without_jobs_returns_false(world, make_worker):
    assert await make_worker().run_once() is False


async def test_git_push_job_syncs_the_repo(world, fake, make_worker):
    fake.commit("kb-all", {"a.md": md("a", "A"), "docs/b.md": md("b", "B")})
    job_id = add_job(world, "git_push", "kb-all")
    assert await make_worker().run_once() is True
    assert job(world, job_id) == ("done", 1, None)
    assert q(world, "SELECT path, audience FROM kh.documents ORDER BY path") == [
        ("a.md", "all"),
        ("docs/b.md", "all"),
    ]
    assert q(world, "SELECT count(*) FROM kh.chunks WHERE document_id IS NOT NULL") == [(2,)]


async def test_git_push_without_repo_fails(world, make_worker):
    job_id = add_job(world, "git_push")
    await make_worker().run_once()
    state, attempts, err = job(world, job_id)
    assert (state, attempts) == ("failed", 1)
    assert "without a repo" in err


async def test_git_push_for_unconfigured_repo_fails(world, make_worker):
    job_id = add_job(world, "git_push", "nope")
    await make_worker().run_once()
    assert job(world, job_id)[0] == "failed"


async def test_two_git_push_jobs_for_one_repo_both_finish(world, fake, make_worker):
    import asyncio

    fake.commit("kb-all", {"a.md": md("a", "A")})
    first, second = add_job(world, "git_push", "kb-all"), add_job(world, "git_push", "kb-all")
    worker = make_worker()
    await asyncio.gather(worker.run_once(), worker.run_once())
    assert job(world, first)[0] == "done" and job(world, second)[0] == "done"
    assert q(world, "SELECT count(*) FROM kh.documents") == [(1,)]
    assert q(world, "SELECT count(*) FROM kh.chunks") == [(1,)]


def add_record(world, audience="all", blob_key="records/r1"):
    return q(
        world,
        "INSERT INTO kh.records (audience, source, source_ref, title, blob_key, excerpt, content_type,"
        " occurred_at, written_by, created_by_sub, created_by_type)"
        " VALUES (%s, 's', %s, 'R', %s, 'x', 'text/plain', now(), 'kh-bot', 'sub', 'n8n_workflow')"
        " RETURNING id::text",
        audience,
        blob_key,
        blob_key,
    )[0][0]


async def test_inserting_a_record_queues_embed_record_and_the_worker_chunks_it(world, blobs, make_worker):
    body = "# Meeting\nWe agreed on alpha.\n\n# Next\nBeta follows.\n"
    await blobs.put("records/r1", body.encode(), "text/plain")
    rec = add_record(world)
    [(job_id, kind, payload)] = q(world, "SELECT id, kind, payload FROM kh.index_jobs")
    assert kind == "embed_record" and payload == {"id": rec}
    await make_worker().run_once()
    assert job(world, job_id) == ("done", 1, None)
    rows = q(
        world,
        "SELECT tier, audience, embedding IS NOT NULL, embedding_model, document_id IS NULL"
        " FROM kh.chunks WHERE record_id = %s ORDER BY ordinal",
        rec,
    )
    assert len(rows) >= 1
    assert all(r == ("T3", "all", True, "fake", True) for r in rows)


async def test_embed_record_replaces_existing_chunks(world, blobs, make_worker):
    await blobs.put("records/r1", b"first body\n", "text/plain")
    rec = add_record(world)
    await make_worker().run_once()
    await blobs.put("records/r1", b"second body\n", "text/plain")
    add_job(world, "embed_record", payload=json.dumps({"id": rec}))
    await make_worker().run_once()
    assert q(world, "SELECT text FROM kh.chunks WHERE record_id = %s", rec) == [("second body",)]


async def test_embed_record_strips_nul_from_a_stored_body(world, blobs, make_worker, caplog):
    await blobs.put("records/r1", b"# Hea\x00d\nal\x00pha beta\n", "text/plain")
    rec = add_record(world)
    [(job_id,)] = q(world, "SELECT id FROM kh.index_jobs")
    with caplog.at_level(logging.WARNING, logger="knowledge_hub.indexer.worker"):
        await make_worker().run_once()
    assert job(world, job_id) == ("done", 1, None)
    chunks = q(world, "SELECT heading_path, text FROM kh.chunks WHERE record_id = %s", rec)
    assert chunks == [("Head", "alpha beta")]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = warning.getMessage()
    assert rec in message and "removed 2 NUL characters" in message
    assert "alpha" not in caplog.text


async def test_embed_record_skips_embedding_when_the_embedder_refuses(world, blobs, indexer_pool, reader):
    class Refusing(FakeEmbedder):
        def may_embed(self, audience):
            return False

    await blobs.put("records/r1", b"private text\n", "text/plain")
    rec = add_record(world)
    worker = Worker(indexer_pool, reader, Refusing(1024), KH_CONFIG, blobs, chunker_version=1)
    await worker.run_once()
    assert q(world, "SELECT embedding IS NULL, embedding_model FROM kh.chunks WHERE record_id = %s", rec) == [
        (True, None)
    ]


async def test_embed_record_for_a_missing_record_is_done(world, make_worker):
    missing = {"id": "00000000-0000-4000-8000-000000000001"}
    job_id = add_job(world, "embed_record", payload=json.dumps(missing))
    await make_worker().run_once()
    assert job(world, job_id)[0] == "done"


async def test_embed_record_without_a_blob_store_fails(world, indexer_pool, reader):
    rec = add_record(world)
    worker = Worker(indexer_pool, reader, FakeEmbedder(1024), KH_CONFIG, None, chunker_version=1)
    await worker.run_once()
    [(state, _, err)] = q(world, "SELECT state, attempts, last_error FROM kh.index_jobs")
    assert state == "failed" and "no blob store" in err
    assert rec


async def test_failing_job_is_retried_with_backoff_and_fails_for_good_after_five_attempts(world, make_worker):
    rec = add_record(world, blob_key="records/missing")  # the blob was never stored
    [(job_id,)] = q(world, "SELECT id FROM kh.index_jobs")
    worker = make_worker()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert await worker.run_once() is True
        state, attempts, err = job(world, job_id)
        assert (state, attempts) == ("failed", attempt)
        assert err and "records/missing" in err
        [(delay,)] = q(
            world,
            "SELECT extract(epoch FROM run_after - now())::int FROM kh.index_jobs WHERE id = %s",
            job_id,
        )
        assert worker_mod.BACKOFF_SECONDS * attempt - 5 <= delay <= worker_mod.BACKOFF_SECONDS * attempt
        # not due yet: nothing to claim
        assert await worker.run_once() is False
        q(world, "UPDATE kh.index_jobs SET run_after = now() WHERE id = %s RETURNING id", job_id)
    assert await worker.run_once() is False  # five attempts used; it stays failed
    assert job(world, job_id)[:2] == ("failed", MAX_ATTEMPTS)
    assert rec


async def test_unknown_job_kind_is_not_possible_in_the_table(world):
    with pytest.raises(psycopg.errors.CheckViolation):
        add_job(world, "pr_event")


async def test_run_forever_works_until_stopped(world, fake, make_worker):
    import asyncio

    fake.commit("kb-all", {"a.md": md("a")})
    job_id = add_job(world, "git_push", "kb-all")
    worker = make_worker()
    task = asyncio.create_task(worker.run_forever())
    try:
        for _ in range(100):
            if job(world, job_id)[0] == "done":
                break
            await asyncio.sleep(0.05)
        assert job(world, job_id)[0] == "done"
    finally:
        worker.stop()
        await asyncio.wait_for(task, 5)


# --- tech repos ------------------------------------------------------------------------------

from tests.support.tech import TECH_CONFIG, TECH_REPO  # noqa: E402


def tech_push(repo=TECH_REPO, ref="refs/heads/trunk", default="trunk", org="kh-tech"):
    repository = {"full_name": f"{org}/{repo}"}
    if default is not None:
        repository["default_branch"] = default
    return {"ref": ref, "repository": repository}


@pytest.fixture
async def tech_http(hook_settings, app_pool, indexer_pool):
    tech = Deps(settings=hook_settings, config=TECH_CONFIG, app_pool=app_pool, indexer_pool=indexer_pool)
    app = build_app(hook_settings, tech, verifier_for_tests())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as client:
        yield client


async def test_tech_push_to_the_default_branch_queues_a_job(world, tech_http):
    r = await post(tech_http, tech_push())
    assert r.status_code == 202
    assert jobs(world) == [("git_push", TECH_REPO, "queued")]
    assert 'kh_webhook_events_total{result="queued"} 1' in METRICS.render()


async def test_tech_push_to_another_branch_is_ignored(world, tech_http):
    assert (await post(tech_http, tech_push(ref="refs/heads/main"))).status_code == 202
    assert (await post(tech_http, tech_push(), event="pull_request")).status_code == 202
    assert jobs(world) == []


async def test_tech_push_without_default_branch_in_payload_is_ignored(world, tech_http):
    assert (await post(tech_http, tech_push(default=None))).status_code == 202
    assert (await post(tech_http, tech_push(default=""))).status_code == 202
    assert jobs(world) == []


async def test_push_to_an_unconfigured_tech_repo_is_ignored(world, tech_http):
    assert (await post(tech_http, tech_push(repo="other-repo"))).status_code == 202
    assert (await post(tech_http, tech_push(org="elsewhere"))).status_code == 202
    assert jobs(world) == []
    # the group path still works and still wants main
    assert (await post(tech_http, push())).status_code == 202
    assert jobs(world) == [("git_push", "kb-all", "queued")]


async def test_tech_push_with_a_bad_signature_is_refused(world, tech_http):
    assert (await post(tech_http, tech_push(), secret="wrong")).status_code == 401
    assert jobs(world) == []


async def test_worker_syncs_a_tech_repo_with_the_tech_reader(world, indexer_pool, reader):
    q(world, "INSERT INTO kh.labels (name) VALUES ('ember') RETURNING name")
    tech_fake = FakeForgejo(org="kh-tech")
    tech_fake.add_repo(TECH_REPO, branch="trunk")
    tech_fake.commit(TECH_REPO, {"docs/a.md": "# Alpha\n\nText here.\n"})
    tech_reader = ForgejoReader("https://forgejo.test", "tok", "kh-tech", transport=tech_fake.transport)
    try:
        # the group reader knows no tech repo: only tech_reader can serve this job
        worker = Worker(
            indexer_pool, reader, FakeEmbedder(1024), TECH_CONFIG, chunker_version=1, tech_reader=tech_reader
        )
        job_id = add_job(world, "git_push", TECH_REPO)
        assert await worker.run_once() is True
    finally:
        await tech_reader.aclose()
    assert job(world, job_id) == ("done", 1, None)
    assert q(world, "SELECT repo, path, tier, audience FROM kh.documents") == [
        (TECH_REPO, "docs/a.md", "T0a", "all")
    ]
