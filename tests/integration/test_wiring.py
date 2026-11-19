"""``build_deps`` over the test cluster, the app, the worker task and ``kh serve`` refusing bad settings."""

from __future__ import annotations

import asyncio

import httpx
import psycopg
import pytest

from knowledge_hub.app import build_app
from knowledge_hub.cli import main
from knowledge_hub.config import KhConfig
from knowledge_hub.deps import build_deps, close_deps
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.forgejo import ForgejoReader
from knowledge_hub.indexer.worker import Worker
from knowledge_hub.mcp.server import Deps
from tests.support.forgejo_fake import FakeForgejo, md
from tests.support.principals import KH_CONFIG


@pytest.fixture
def wiring_settings(db_settings):
    return db_settings.model_copy(
        update={
            "env": "test",
            "embed_provider": "fake",
        }
    )


@pytest.fixture
def forgejo_settings(wiring_settings, tmp_path):
    token = tmp_path / "indexer"
    token.write_text("indexer-token-for-test\n")
    return wiring_settings.model_copy(
        update={"forgejo_url": "https://forgejo.test.example", "forgejo_indexer_token_file": token}
    )


async def test_build_deps_returns_working_dependencies(wiring_settings):
    deps = await build_deps(wiring_settings, KhConfig())
    try:
        for pool in (deps.app_pool, deps.indexer_pool):
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT 1")
                assert (await cur.fetchone())[0] == 1
        assert isinstance(deps.embedder, FakeEmbedder)
        assert deps.blobs is None
        assert deps.reader is None and deps.worker is None
        assert deps.bearer is None
    finally:
        await close_deps(deps)


async def test_build_deps_builds_reader_and_worker_when_forgejo_url_is_set(forgejo_settings):
    deps = await build_deps(forgejo_settings, KhConfig(org="acme"))
    try:
        assert isinstance(deps.reader, ForgejoReader)
        assert deps.reader._http.headers["Authorization"] == "token indexer-token-for-test"
        assert isinstance(deps.worker, Worker)
        assert deps.worker.reader is deps.reader and deps.worker.pool is deps.indexer_pool
        assert deps.worker.chunker_version == forgejo_settings.chunker_version
        http = deps.reader._http
    finally:
        await close_deps(deps)
    assert http.is_closed


async def test_build_deps_needs_the_indexer_token_file_with_forgejo_url(forgejo_settings):
    settings = forgejo_settings.model_copy(update={"forgejo_indexer_token_file": None})
    with pytest.raises(ValueError, match="KH_FORGEJO_INDEXER_TOKEN_FILE"):
        await build_deps(settings, KhConfig())


async def test_app_from_build_deps_answers_healthz(wiring_settings):
    deps = await build_deps(wiring_settings, KhConfig())
    app = build_app(wiring_settings, deps)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                resp = await http.get("/healthz")
        assert resp.status_code == 200
        assert resp.text == "ok"
    finally:
        await close_deps(deps)


async def test_webhook_route_is_post_only(wiring_settings):
    deps = await build_deps(wiring_settings, KhConfig())
    app = build_app(wiring_settings, deps)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            assert (await http.get("/webhooks/forgejo")).status_code == 405
            assert (await http.post("/webhooks/forgejo", content=b"{}")).status_code == 401
    finally:
        await close_deps(deps)


async def test_lifespan_runs_the_worker_and_stops_it(wiring_settings, fresh_db, app_pool, indexer_pool):
    with psycopg.connect(fresh_db.admin_dsn("kh_fresh"), autocommit=True) as conn:
        for name in KH_CONFIG.groups:
            conn.execute(
                "INSERT INTO kh.groups (name, repo) VALUES (%s, %s)", (name, KH_CONFIG.repo_for_group(name))
            )
        conn.execute(
            "INSERT INTO kh.index_jobs (kind, repo) VALUES ('git_push', 'kb-all') RETURNING id"
        )
    fake = FakeForgejo()
    fake.add_repo("kb-all")
    fake.commit("kb-all", {"a.md": md("a", "A")})
    reader = ForgejoReader("https://forgejo.test", "tok", "kh", transport=fake.transport)
    worker = Worker(
        indexer_pool, reader, FakeEmbedder(1024), KH_CONFIG, None, chunker_version=1, poll_seconds=0.05
    )
    deps = Deps(
        settings=wiring_settings, config=KH_CONFIG, app_pool=app_pool, indexer_pool=indexer_pool,
        reader=reader, worker=worker,
    )
    app = build_app(wiring_settings, deps)
    try:
        async with app.router.lifespan_context(app):
            for _ in range(100):
                with psycopg.connect(fresh_db.admin_dsn("kh_fresh"), autocommit=True) as conn:
                    state = conn.execute("SELECT state FROM kh.index_jobs").fetchone()[0]
                if state == "done":
                    break
                await asyncio.sleep(0.05)
            assert state == "done"
        assert worker._stop.is_set()  # the lifespan asked the worker to stop
        with psycopg.connect(fresh_db.admin_dsn("kh_fresh"), autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM kh.documents").fetchone()[0] == 1
    finally:
        await reader.aclose()


def test_serve_with_invalid_setting_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("KH_ENV", "prod")
    monkeypatch.setenv("KH_EMBED_PROVIDER", "fake")
    assert main(["serve"]) == 2
    assert "KH_EMBED_PROVIDER" in capsys.readouterr().err
