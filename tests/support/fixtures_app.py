"""Application fixtures: the app running in process over the migrated test database."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from knowledge_hub.app import build_app
from knowledge_hub.config import Config
from knowledge_hub.mcp.server import Deps
from knowledge_hub.settings import Settings
from tests.support.claims import verifier_for_tests
from tests.support.mcp_client import BASE_URL, HOST, McpClient


@pytest.fixture
def app_settings(db_settings) -> Settings:
    return db_settings.model_copy(update={"env": "test", "allowed_hosts": HOST})


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool) -> Deps:
    return Deps(settings=app_settings, config=Config(), app_pool=app_pool, indexer_pool=indexer_pool)


@pytest.fixture
async def mcp_client(app_settings, deps):
    app = build_app(app_settings, deps, verifier_for_tests())
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan() -> None:
        # The SDK's session manager holds an anyio task group, which must be entered and left in
        # one task; pytest's fixture setup and teardown run in different ones.
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    await asyncio.wait_for(started.wait(), 10)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            yield McpClient(http)
    finally:
        stop.set()
        await task
