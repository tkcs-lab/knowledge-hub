"""HTTP routes of the OAuth server (plan 3.9): metadata, SDK routes, callback, consent, limiters."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import (
    build_metadata,
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from knowledge_hub.oauth.consent import consent_headers, render_consent
from knowledge_hub.oauth.flow import AuthorizationFlow, FlowError
from knowledge_hub.ratelimit import RateLimiter

SCOPES = ["kh:read", "kh:write"]
MAX_BODY = 16 * 1024
INVALID = "invalid request"


class _TooLarge(Exception):
    pass


class _limited:  # noqa: N801 - a callable ASGI wrapper, not a function (Starlette would treat a function as a view)
    def __init__(self, app: ASGIApp, limiter: RateLimiter, key: str, now: Callable[[], float]) -> None:
        self.app, self.limiter, self.key, self.now = app, limiter, key, now

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") != "OPTIONS":
            allowed, retry = self.limiter.allow(self.key, self.now())
            if not allowed:
                resp = PlainTextResponse(
                    "too many requests", status_code=429, headers={"Retry-After": str(retry)}
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise _TooLarge
    size, chunks = 0, []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _bad() -> Response:
    return PlainTextResponse(INVALID, status_code=400, headers={"Cache-Control": "no-store"})


def _redirect(url: str) -> Response:
    return Response(status_code=302, headers={"Location": url, "Cache-Control": "no-store"})


def build_oauth_routes(
    settings: Any, provider: Any, flow: AuthorizationFlow, *, now: Callable[[], float] = time.time
) -> list[BaseRoute]:
    issuer = AnyHttpUrl(settings.public_url)
    secure = str(settings.public_url).startswith("https://")
    cookie_name = "__Host-kh_flow" if secure else "kh_flow"
    register_limiter = RateLimiter(settings.oauth_register_per_min)
    authorize_limiter = RateLimiter(settings.oauth_authorize_per_min)

    registration = ClientRegistrationOptions(
        enabled=bool(settings.oauth_dcr), valid_scopes=SCOPES, default_scopes=SCOPES
    )
    revocation = RevocationOptions(enabled=True)

    metadata = build_metadata(issuer, None, registration, revocation)
    metadata.token_endpoint_auth_methods_supported = ["none"]
    metadata.revocation_endpoint_auth_methods_supported = ["none"]

    routes: list[BaseRoute] = [
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )
    ]

    for route in create_auth_routes(
        provider, issuer_url=issuer, client_registration_options=registration, revocation_options=revocation
    ):
        if route.path == "/.well-known/oauth-authorization-server":
            continue
        if route.path == "/register":
            if not registration.enabled:
                continue
            app = _limited(route.endpoint, register_limiter, "register", now)
        elif route.path == "/authorize":
            app = _limited(route.endpoint, authorize_limiter, "authorize", now)
        else:
            app = route.endpoint
        routes.append(Route(route.path, endpoint=app, methods=sorted(route.methods or [])))

    routes.extend(
        create_protected_resource_routes(
            resource_url=AnyHttpUrl(settings.resource_url),
            authorization_servers=[issuer],
            scopes_supported=SCOPES,
        )
    )

    def set_cookie(resp: Response, ticket: str) -> None:
        resp.set_cookie(
            cookie_name, ticket, max_age=600, path="/", secure=secure, httponly=True, samesite="lax"
        )

    def clear_cookie(resp: Response) -> None:
        resp.delete_cookie(cookie_name, path="/", secure=secure, httponly=True, samesite="lax")

    async def callback(request: Request) -> Response:
        q = request.query_params
        try:
            outcome = await flow.handle_callback(q.get("code"), q.get("state"), q.get("error"))
        except FlowError:
            return _bad()
        if outcome.redirect_url:
            return _redirect(outcome.redirect_url)
        resp = _redirect("/oauth/consent")
        set_cookie(resp, outcome.consent_ticket or "")
        return resp

    async def consent_get(request: Request) -> Response:
        try:
            ctx = await flow.consent_context(request.cookies.get(cookie_name))
        except FlowError:
            return _bad()
        return HTMLResponse(
            render_consent(ctx.client_name, ctx.redirect_host, ctx.email, ctx.csrf),
            headers=consent_headers(ctx.redirect_origin),
        )

    async def consent_post(request: Request) -> Response:
        try:
            body = await _read_body(request)
        except _TooLarge:
            return PlainTextResponse("payload too large", status_code=413)
        form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        csrf = (form.get("csrf") or [None])[0]
        decision = (form.get("decision") or [None])[0]
        try:
            if decision not in ("allow", "deny"):
                raise FlowError("bad decision")
            url = await flow.decide_consent(request.cookies.get(cookie_name), csrf, decision == "allow")
        except FlowError:
            resp = _bad()
            clear_cookie(resp)
            return resp
        resp = _redirect(url)
        clear_cookie(resp)
        return resp

    def shared(endpoint: Callable[..., Any]) -> ASGIApp:
        from starlette.routing import request_response

        return _limited(request_response(endpoint), authorize_limiter, "authorize", now)

    routes.append(Route("/oauth/callback", endpoint=shared(callback), methods=["GET"]))
    routes.append(Route("/oauth/consent", endpoint=shared(consent_get), methods=["GET"]))
    routes.append(Route("/oauth/consent", endpoint=shared(consent_post), methods=["POST"]))
    return routes
