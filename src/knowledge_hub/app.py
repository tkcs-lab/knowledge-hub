"""The HTTP application: ``/mcp`` behind the caller middleware, ``/healthz``, the webhook, ``/metrics``."""

import asyncio
import contextlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from knowledge_hub.auth.middleware import CallerMiddleware
from knowledge_hub.indexer.webhook import make_webhook_endpoint
from knowledge_hub.mcp.server import Deps, build_mcp
from knowledge_hub.metrics import JOB_STATES, METRICS
from knowledge_hub.settings import Settings


def allowed_hosts(settings: Settings) -> list[str]:
    return [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]


async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def make_metrics_endpoint(settings: Settings, deps: Deps):
    async def metrics(request: Request) -> Response:
        path = settings.metrics_token_file
        try:
            expected = path.read_text().strip().encode() if path is not None else b""
        except OSError:
            expected = b""
        header = request.headers.get("authorization", "")
        scheme, _, given = header.partition(" ")
        if not expected or scheme.lower() != "bearer" or not hmac.compare_digest(given.encode(), expected):
            return Response(status_code=401, headers={"www-authenticate": "Bearer"})
        states: dict[str, int] | None = None
        if deps.indexer_pool is not None:
            try:
                async with deps.indexer_pool.connection() as conn:
                    cur = await conn.execute("SELECT state, count(*) FROM kh.index_jobs GROUP BY state")
                    counts = {s: int(n) for s, n in await cur.fetchall()}
                states = {s: counts.get(s, 0) for s in JOB_STATES}
            except Exception:
                states = None
        return PlainTextResponse(
            METRICS.render(states), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    return metrics


def build_app(settings: Settings, deps: Deps) -> Starlette:
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
        holder["middleware"] = CallerMiddleware(mcp_handler, deps.bearer, settings, config=deps.config)
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

    routes = [
        Route("/mcp", endpoint=McpRoute()),
        Route("/healthz", healthz),
        Route("/webhooks/forgejo", make_webhook_endpoint(deps), methods=["POST"]),
    ]
    if settings.metrics_token_file is not None:
        routes.append(Route("/metrics", make_metrics_endpoint(settings, deps), methods=["GET"]))
    return Starlette(
        routes=routes,
        lifespan=lifespan,
    )
