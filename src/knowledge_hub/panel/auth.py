"""Sign-in routes of the panel and the per-request admin check."""

from __future__ import annotations

import hmac
import logging
import secrets

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from knowledge_hub.config import ConfigError
from knowledge_hub.oauth.upstream import UpstreamError
from knowledge_hub.panel.context import PanelContext

log = logging.getLogger(__name__)

FLOW_COOKIE = "kh_panel_flow"
SESSION_COOKIE = "kh_panel"
FLOW_TTL = 600

REFUSED = "sign-in refused"
INVALID = "invalid request"
NOT_ADMIN = "not an administrator"
CONFIG_INVALID = "panel unavailable: kh.yaml is invalid"

CALLBACK_PAGE = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8">'
    '<meta http-equiv="refresh" content="0;url=/">'
    "<title>Signed in</title></head>"
    '<body><p><a href="/">Continue</a></p></body></html>\n'
)

_NO_STORE = {"Cache-Control": "no-store"}


def _text(text: str, status: int) -> Response:
    return PlainTextResponse(text, status_code=status, headers=_NO_STORE)


def _clear_flow(ctx: PanelContext, resp: Response) -> None:
    resp.delete_cookie(
        ctx.cookie_name(FLOW_COOKIE), path="/", secure=ctx.secure, httponly=True, samesite="lax"
    )


def _refuse(ctx: PanelContext, text: str, status: int, why: str) -> Response:
    log.warning("panel sign-in refused: %s", why)
    resp = _text(text, status)
    _clear_flow(ctx, resp)
    return resp


def current_admin(request: Request, ctx: PanelContext) -> str | None:
    dev = ctx.settings.dev_admin
    if dev:
        return dev
    if ctx.signer is None:
        return None
    token = request.cookies.get(ctx.cookie_name(SESSION_COOKIE))
    if not token:
        return None
    data = ctx.signer.verify("session", token, ctx.now())
    email = data.get("email") if data else None
    if not isinstance(email, str) or not email:
        return None
    try:
        config = ctx.load_config()
    except ConfigError:
        return None
    email = email.lower()
    return email if email in config.admins else None


def build_auth_routes(ctx: PanelContext) -> list[Route]:
    async def login(request: Request) -> Response:
        if ctx.signer is None or ctx.upstream is None:
            return _text(REFUSED, 403)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier, challenge = ctx.upstream.new_pkce()
        try:
            url = await ctx.upstream.authorization_url(state, nonce, challenge)
        except UpstreamError as exc:
            return _refuse(ctx, REFUSED, 403, exc.code)
        token = ctx.signer.sign(
            "flow", {"state": state, "nonce": nonce, "verifier": verifier}, FLOW_TTL, ctx.now()
        )
        resp = Response(status_code=302, headers={"Location": url, **_NO_STORE})
        resp.set_cookie(
            ctx.cookie_name(FLOW_COOKIE),
            token,
            max_age=FLOW_TTL,
            path="/",
            secure=ctx.secure,
            httponly=True,
            samesite="lax",
        )
        return resp

    async def callback(request: Request) -> Response:
        if ctx.signer is None or ctx.upstream is None:
            return _text(REFUSED, 403)
        q = request.query_params
        cookie = request.cookies.get(ctx.cookie_name(FLOW_COOKIE))
        flow = ctx.signer.verify("flow", cookie, ctx.now()) if cookie else None
        state = q.get("state")
        want = flow.get("state") if flow else None
        nonce = flow.get("nonce") if flow else None
        verifier = flow.get("verifier") if flow else None
        if not (
            isinstance(want, str)
            and isinstance(nonce, str)
            and isinstance(verifier, str)
            and state
            and hmac.compare_digest(state.encode(), want.encode())
        ):
            return _refuse(ctx, INVALID, 400, "bad flow cookie or state")
        code = q.get("code")
        if q.get("error"):
            return _refuse(ctx, REFUSED, 403, f"idp_error:{q.get('error')[:40]}")
        if not code:
            return _refuse(ctx, REFUSED, 403, "missing_code")
        try:
            tokens = await ctx.upstream.exchange(code, verifier)
            id_token = tokens.get("id_token")
            if not isinstance(id_token, str):
                raise UpstreamError("id_token_missing")
            identity = await ctx.upstream.verify_id_token(id_token, nonce)
        except UpstreamError as exc:
            return _refuse(ctx, REFUSED, 403, exc.code)
        try:
            config = ctx.load_config()
        except ConfigError:
            return _refuse(ctx, CONFIG_INVALID, 403, "kh.yaml invalid")
        email = identity.email.strip().lower()
        if email not in config.admins:
            return _refuse(ctx, NOT_ADMIN, 403, "not an administrator")
        now = ctx.now()
        ttl = ctx.settings.session_hours * 3600
        token = ctx.signer.sign("session", {"email": email, "iat": int(now)}, ttl, now)
        resp = Response(CALLBACK_PAGE, media_type="text/html", headers=_NO_STORE)
        _clear_flow(ctx, resp)
        resp.set_cookie(
            ctx.cookie_name(SESSION_COOKIE),
            token,
            max_age=ttl,
            path="/",
            secure=ctx.secure,
            httponly=True,
            samesite="strict",
        )
        return resp

    async def logout(request: Request) -> Response:
        resp = Response(status_code=204, headers=_NO_STORE)
        resp.delete_cookie(
            ctx.cookie_name(SESSION_COOKIE),
            path="/",
            secure=ctx.secure,
            httponly=True,
            samesite="strict",
        )
        return resp

    return [
        Route("/auth/login", login, methods=["GET"]),
        Route("/auth/callback", callback, methods=["GET"]),
        Route("/api/logout", logout, methods=["POST"]),
    ]
