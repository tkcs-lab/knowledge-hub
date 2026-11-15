"""The HTTP application: ``/mcp`` behind the claim middleware, ``/healthz``. Later phases add routes."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from knowledge_hub.auth.claims import ClaimMiddleware
from knowledge_hub.auth.pf_claim_verify import ClaimVerifier
from knowledge_hub.auth.replay import PgReplayCache
from knowledge_hub.indexer.webhook import make_webhook_endpoint
from knowledge_hub.jobs.audience_map import load_audience_map
from knowledge_hub.mcp.server import Deps, build_mcp
from knowledge_hub.settings import Settings


def allowed_hosts(settings: Settings) -> list[str]:
    return [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]


async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def build_app(settings: Settings, deps: Deps, verifier: ClaimVerifier) -> Starlette:
    """Root app. The caller opens ``deps.app_pool`` before the first request."""
    mcp = build_mcp(deps)
    sdk_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts(settings),
            allowed_origins=[],
        ),
    )
    mcp_handler = next(r for r in sdk_app.routes if getattr(r, "path", None) == "/mcp").endpoint
    holder: dict[str, Any] = {}

    class McpRoute:
        """ASGI app (not a function, so Starlette does not wrap it as a request handler)."""

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await holder["middleware"](scope, receive, send)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        holder["middleware"] = ClaimMiddleware(
            mcp_handler, verifier, settings, PgReplayCache(deps.app_pool)
        )
        if deps.indexer_pool is not None:
            await load_audience_map(deps.indexer_pool, deps.config)
        task = asyncio.create_task(deps.worker.run_forever()) if deps.worker is not None else None
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if task is not None:
                deps.worker.stop()
                try:
                    await asyncio.wait_for(task, 10)
                except (TimeoutError, asyncio.CancelledError):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    return Starlette(
        routes=[
            Route("/mcp", endpoint=McpRoute()),
            Route("/healthz", healthz),
            Route("/webhooks/forgejo", make_webhook_endpoint(deps), methods=["POST"]),
        ],
        lifespan=lifespan,
    )
