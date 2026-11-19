"""OAuth routes, metadata, consent page and limiters, over ASGI with a stub provider and flow."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from knowledge_hub.oauth.consent import consent_headers, render_consent
from knowledge_hub.oauth.flow import CallbackOutcome, ConsentContext, FlowError
from knowledge_hub.oauth.routes import build_oauth_routes

pytestmark = pytest.mark.unit

PUBLIC = "https://hub.example.test"


class StubProvider:
    async def get_client(self, client_id: str) -> None:
        return None

    async def register_client(self, info: Any) -> None:
        return None

    async def authorize(self, client: Any, params: Any) -> str:
        return "https://idp.example.test/auth"


class StubFlow:
    def __init__(self) -> None:
        self.callback: Any = CallbackOutcome(consent_ticket="tkt", csrf="tok")
        self.name = "Claude"
        self.decided: list[tuple[Any, Any, bool]] = []
        self.error = False

    async def handle_callback(self, code, state, error):
        if self.error:
            raise FlowError("x")
        return self.callback

    async def consent_context(self, ticket):
        if not ticket or self.error:
            raise FlowError("x")
        return ConsentContext(self.name, "claude.ai", "https://claude.ai", "a@example.test", "tok")

    async def decide_consent(self, ticket, csrf, allow):
        if csrf != "tok" or not ticket:
            raise FlowError("csrf")
        self.decided.append((ticket, csrf, allow))
        return "https://claude.ai/cb?code=c"


def settings(**kw: Any) -> SimpleNamespace:
    base = {
        "public_url": PUBLIC,
        "resource_url": f"{PUBLIC}/mcp",
        "oauth_dcr": True,
        "oauth_register_per_min": 2,
        "oauth_authorize_per_min": 3,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def make(flow: StubFlow | None = None, clock: list[float] | None = None, **kw: Any):
    flow = flow or StubFlow()
    clock = clock if clock is not None else [1000.0]
    routes = build_oauth_routes(settings(**kw), StubProvider(), flow, now=lambda: clock[0])
    base = str(kw.get("public_url", PUBLIC))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=routes)), base_url=base)
    return client, flow, clock


async def test_metadata_documents() -> None:
    client, _, _ = make()
    async with client:
        am = (await client.get("/.well-known/oauth-authorization-server")).json()
        pr = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert am["issuer"] == pr["authorization_servers"][0]
    assert am["authorization_endpoint"].endswith("/authorize")
    assert am["token_endpoint"].endswith("/token")
    assert am["registration_endpoint"].endswith("/register")
    assert "S256" in am["code_challenge_methods_supported"]
    assert am["token_endpoint_auth_methods_supported"] == ["none"]
    assert am["revocation_endpoint_auth_methods_supported"] == ["none"]
    assert pr["resource"].rstrip("/") == f"{PUBLIC}/mcp"
    assert set(pr["scopes_supported"]) == {"kh:read", "kh:write"}


async def test_no_registration_without_dcr() -> None:
    client, _, _ = make(oauth_dcr=False)
    async with client:
        am = (await client.get("/.well-known/oauth-authorization-server")).json()
        r = await client.post("/register", json={})
    assert "registration_endpoint" not in am
    assert r.status_code in (404, 405)


async def test_register_limiter() -> None:
    client, _, clock = make()
    async with client:
        codes = [(await client.post("/register", json={})).status_code for _ in range(2)]
        assert 429 not in codes
        r = await client.post("/register", json={})
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        clock[0] += 120
        assert (await client.post("/register", json={})).status_code != 429


async def test_shared_limiter_covers_hub_routes() -> None:
    client, _, clock = make()
    async with client:
        assert (await client.get("/oauth/callback", params={"state": "s"})).status_code == 302
        assert (await client.get("/oauth/consent", cookies={"__Host-kh_flow": "t"})).status_code == 200
        assert (await client.get("/authorize")).status_code != 429
        for path in ("/oauth/callback", "/oauth/consent", "/authorize"):
            assert (await client.get(path)).status_code == 429
        assert (await client.post("/oauth/consent", data={})).status_code == 429
        clock[0] += 120
        assert (await client.get("/oauth/callback", params={"state": "s"})).status_code == 302


async def test_callback_sets_cookie_and_redirects() -> None:
    client, _, _ = make()
    async with client:
        r = await client.get("/oauth/callback", params={"state": "s", "code": "c"})
    assert r.status_code == 302 and r.headers["location"] == "/oauth/consent"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-kh_flow=tkt")
    for part in ("HttpOnly", "SameSite=lax", "Path=/", "Secure"):
        assert part.lower() in cookie.lower()


async def test_cookie_over_http() -> None:
    client, _, _ = make(public_url="http://localhost:8080", resource_url="http://localhost:8080/mcp")
    async with client:
        r = await client.get("/oauth/callback", params={"state": "s"})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("kh_flow=tkt") and "secure" not in cookie.lower()


async def test_callback_redirect_outcome() -> None:
    flow = StubFlow()
    flow.callback = CallbackOutcome(redirect_url="https://claude.ai/cb?error=access_denied")
    client, _, _ = make(flow)
    async with client:
        r = await client.get("/oauth/callback", params={"state": "s"})
    assert r.status_code == 302 and "set-cookie" not in r.headers
    assert r.headers["location"] == "https://claude.ai/cb?error=access_denied"


async def test_callback_flow_error() -> None:
    flow = StubFlow()
    flow.error = True
    client, _, _ = make(flow)
    async with client:
        r = await client.get("/oauth/callback", params={"state": "s"})
    assert r.status_code == 400 and r.text == "invalid request" and "set-cookie" not in r.headers


async def test_consent_page_headers_and_escape() -> None:
    flow = StubFlow()
    flow.name = "<script>alert(1)</script>"
    client, _, _ = make(flow)
    async with client:
        r = await client.get("/oauth/consent", cookies={"__Host-kh_flow": "t"})
    assert r.status_code == 200
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text
    assert "form-action 'self' https://claude.ai" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


async def test_consent_get_without_cookie_is_400() -> None:
    client, _, _ = make()
    async with client:
        assert (await client.get("/oauth/consent")).status_code == 400


def test_consent_headers_and_page() -> None:
    h = consent_headers("https://x.test:8443")
    assert set(h) == {"Content-Security-Policy", "X-Frame-Options", "Referrer-Policy", "Cache-Control"}
    assert "form-action 'self' https://x.test:8443;" in h["Content-Security-Policy"]
    page = render_consent("n", "h", "e@x", 'a"b')
    assert "<title>Knowledge hub: allow access</title>" in page
    assert 'name="csrf" value="a&quot;b"' in page
    assert 'name="decision" value="allow"' in page and 'name="decision" value="deny"' in page


async def test_post_decision() -> None:
    client, flow, _ = make()
    async with client:
        r = await client.post(
            "/oauth/consent", data={"csrf": "tok", "decision": "allow"}, cookies={"__Host-kh_flow": "t"}
        )
    assert r.status_code == 302 and r.headers["location"] == "https://claude.ai/cb?code=c"
    assert flow.decided == [("t", "tok", True)]
    assert "kh_flow=" in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()


async def test_post_bad_csrf_does_not_issue() -> None:
    client, flow, _ = make()
    async with client:
        missing = await client.post(
            "/oauth/consent", data={"decision": "allow"}, cookies={"__Host-kh_flow": "t"}
        )
        wrong = await client.post(
            "/oauth/consent", data={"csrf": "nope", "decision": "allow"}, cookies={"__Host-kh_flow": "t"}
        )
    assert missing.status_code == 400 and wrong.status_code == 400
    assert flow.decided == []


async def test_post_oversized_is_413() -> None:
    client, flow, _ = make()
    async with client:
        r = await client.post(
            "/oauth/consent",
            content=b"csrf=tok&decision=allow&pad=" + b"a" * 20000,
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies={"__Host-kh_flow": "t"},
        )
    assert r.status_code == 413 and flow.decided == []
