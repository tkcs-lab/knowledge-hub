"""``build_deps`` over the test cluster, the app built from it, and ``kh serve`` refusing bad settings."""

from __future__ import annotations

import httpx
import pytest

from knowledge_hub.app import build_app
from knowledge_hub.cli import main
from knowledge_hub.config import Config
from knowledge_hub.deps import build_deps, close_deps
from knowledge_hub.embed.base import FakeEmbedder


@pytest.fixture
def wiring_settings(db_settings):
    return db_settings.model_copy(
        update={
            "env": "test",
            "embed_provider": "fake",
            "pf_issuer": "https://pf.test.example",
            "pf_audience": "https://hub.test.example/mcp",
            "pf_jwks_url": "https://pf.test.example/.well-known/jwks.json",
        }
    )


async def test_build_deps_returns_working_dependencies(wiring_settings):
    deps = await build_deps(wiring_settings, Config())
    try:
        for pool in (deps.app_pool, deps.indexer_pool):
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT 1")
                assert (await cur.fetchone())[0] == 1
        assert isinstance(deps.embedder, FakeEmbedder)
        assert deps.blobs is None
        assert deps.forgejo is None
        assert deps.verifier is not None
    finally:
        await close_deps(deps)


async def test_build_deps_without_jwks_url_is_a_settings_error(wiring_settings):
    settings = wiring_settings.model_copy(update={"pf_jwks_url": None})
    with pytest.raises(ValueError, match="KH_PF_JWKS_URL"):
        await build_deps(settings, Config())


async def test_app_from_build_deps_answers_healthz(wiring_settings):
    deps = await build_deps(wiring_settings, Config())
    app = build_app(wiring_settings, deps, deps.verifier)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                resp = await http.get("/healthz")
        assert resp.status_code == 200
        assert resp.text == "ok"
    finally:
        await close_deps(deps)


def test_serve_with_invalid_setting_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("KH_ENV", "prod")
    monkeypatch.setenv("KH_EMBED_PROVIDER", "fake")
    assert main(["serve"]) == 2
    assert "KH_EMBED_PROVIDER" in capsys.readouterr().err


async def test_build_deps_bot_client_only_when_bot_token_file_is_set(wiring_settings, tmp_path):
    indexer, bot = tmp_path / "indexer", tmp_path / "bot"
    indexer.write_text("indexer-token-for-test\n")
    bot.write_text("bot-token-for-test\n")
    base = wiring_settings.model_copy(
        update={"forgejo_url": "https://forgejo.test.example", "forgejo_indexer_token_file": indexer}
    )
    deps = await build_deps(base, Config())
    try:
        assert deps.forgejo is not None and deps.forgejo_bot is None
    finally:
        await close_deps(deps)
    deps = await build_deps(base.model_copy(update={"forgejo_bot_token_file": bot}), Config())
    try:
        assert deps.forgejo is not None and deps.forgejo_bot is not None
        assert deps.forgejo_bot is not deps.forgejo
        assert deps.forgejo_bot._http.headers["Authorization"] == "token bot-token-for-test"
        assert deps.forgejo._http.headers["Authorization"] == "token indexer-token-for-test"
    finally:
        await close_deps(deps)
