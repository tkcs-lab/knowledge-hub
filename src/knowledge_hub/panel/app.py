"""The panel's Starlette app: four middlewares, health, the static shell and the route builders."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from knowledge_hub.panel.api_health import build_health_routes
from knowledge_hub.panel.api_read import build_read_routes
from knowledge_hub.panel.auth import build_auth_routes, current_admin
from knowledge_hub.panel.context import PanelContext

log = logging.getLogger(__name__)
audit_log = logging.getLogger("knowledge_hub.panel.audit")

STATIC_DIR = Path(__file__).parent / "static"

ROUTE_BUILDERS: tuple[Callable[[PanelContext], list[Route]], ...] = (
    build_auth_routes,
    build_read_routes,
    build_health_routes,
)

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
PUBLIC_PATHS = ("/healthz", "/auth/login", "/auth/callback")

CallNext = Callable[[Request], Awaitable[Response]]


def _error(status: int, code: str, detail: str = "") -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status)


class ErrorsAndHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        try:
            response = await call_next(request)
        except Exception:
            log.exception("panel request failed: %s %s", request.method, request.url.path)
            response = _error(500, "internal")
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


class HostMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, ctx: PanelContext) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.ctx = ctx

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        public = self.ctx.settings.public_url
        if public and request.url.path != "/healthz":
            want = urlsplit(public).netloc.lower()
            if request.headers.get("host", "").lower() != want:
                return _error(400, "bad_host", "unexpected Host header")
        return await call_next(request)


class AuthGateMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, ctx: PanelContext) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.ctx = ctx

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        path = request.url.path
        request.state.email = None
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        email = current_admin(request, self.ctx)
        if email is None:
            if path == "/":
                return Response(status_code=302, headers={"Location": "/auth/login"})
            if path.startswith("/api/"):
                return _error(401, "unauthorized", "sign in required")
            return await call_next(request)
        request.state.email = email
        return await call_next(request)


class CsrfMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, ctx: PanelContext) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.ctx = ctx

    def _origin(self, request: Request) -> str:
        public = self.ctx.settings.public_url
        if public:
            parts = urlsplit(public)
            return f"{parts.scheme}://{parts.netloc}"
        return "http://" + request.headers.get("host", "")

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        if request.method not in UNSAFE_METHODS:
            return await call_next(request)
        h = request.headers
        site = h.get("sec-fetch-site")
        ctype = h.get("content-type", "").split(";")[0].strip().lower()
        if (
            h.get("origin") != self._origin(request)
            or (site is not None and site != "same-origin")
            or ctype != "application/json"
        ):
            return _error(403, "csrf", "cross-site request refused")
        response = await call_next(request)
        audit_log.info(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "email": getattr(request.state, "email", None),
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                }
            )
        )
        return response


def _static_file(path: str) -> Path | None:
    base = os.path.realpath(STATIC_DIR)
    full = os.path.realpath(os.path.join(base, path))
    if os.path.commonpath([base, full]) != base or not os.path.isfile(full):
        return None
    return Path(full)


def build_panel_app(ctx: PanelContext) -> Starlette:
    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def index(request: Request) -> Response:
        page = _static_file("index.html")
        if page is None:
            return _error(404, "not_found")
        return FileResponse(page, media_type="text/html")

    async def static(request: Request) -> Response:
        rel = request.path_params["path"]
        if "\x00" in rel:
            return _error(404, "not_found")
        found = _static_file(rel)
        if found is None:
            return _error(404, "not_found")
        ctype = mimetypes.guess_type(found.name)[0] or "application/octet-stream"
        return FileResponse(found, media_type=ctype)

    routes: list[Route] = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/", index, methods=["GET"]),
        Route("/static/{path:path}", static, methods=["GET"]),
    ]
    for builder in ROUTE_BUILDERS:
        routes.extend(builder(ctx))
    middleware = [
        Middleware(ErrorsAndHeadersMiddleware),
        Middleware(HostMiddleware, ctx=ctx),
        Middleware(AuthGateMiddleware, ctx=ctx),
        Middleware(CsrfMiddleware, ctx=ctx),
    ]
    return Starlette(routes=routes, middleware=middleware)
