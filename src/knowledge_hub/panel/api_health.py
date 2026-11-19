"""``GET /api/health``."""

from __future__ import annotations

from datetime import UTC, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.health import run_checks


def build_health_routes(ctx: PanelContext) -> list[Route]:
    async def health(request: Request) -> Response:
        checks = await run_checks(ctx)
        return JSONResponse(
            {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "checks": checks}
        )

    return [Route("/api/health", health, methods=["GET"])]
