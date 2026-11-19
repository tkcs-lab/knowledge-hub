"""AuthorizationFlow against a fake store and the fake IdP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from knowledge_hub.oauth.flow import AuthorizationFlow, CallbackOutcome, ConsentContext, FlowError
from knowledge_hub.oauth.store import PendingRow
from knowledge_hub.oauth.tokens import new_secret, token_hash
from knowledge_hub.oauth.upstream import UpstreamClient, UpstreamConfig
from tests.support.fake_idp import FIXED_CODE, make_fake_idp

pytestmark = pytest.mark.unit

ISSUER = "https://idp.example.test"
CLIENT_ID = "kh-client"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
EMAIL = "alice@example.test"


class FakeStore:
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.pending: dict[tuple[bytes, str], tuple[PendingRow, datetime]] = {}
        self.codes: dict[bytes, tuple[str, str, dict[str, Any], datetime]] = {}
        self.consents: set[tuple[str, str]] = set()
        self.authorized: list[str] = []
        self.clients: dict[str, dict[str, Any]] = {"c1": {"client_name": "Claude"}}

    def _live(self, key: tuple[bytes, str]) -> PendingRow | None:
        hit = self.pending.get(key)
        if hit is None or hit[1].timestamp() <= self.clock[0]:
            return None
        return hit[0]

    async def pending_put(self, h, kind, client_id, params, nonce, verifier, expires_at):
        self.pending[(h, kind)] = (PendingRow(client_id, params, nonce, verifier), expires_at)

    async def pending_take(self, h, kind):
        row = self._live((h, kind))
        self.pending.pop((h, kind), None)
        return row

    async def pending_peek(self, h, kind):
        return self._live((h, kind))

    async def code_put(self, h, client_id, email, params, expires_at):
        self.codes[h] = (client_id, email, params, expires_at)

    async def consent_has(self, email, client_id):
        return (email, client_id) in self.consents

    async def consent_put(self, email, client_id):
        self.consents.add((email, client_id))

    async def client_authorize(self, client_id):
        self.authorized.append(client_id)

    async def client_get(self, client_id):
        return self.clients.get(client_id)


@dataclass
class Env:
    flow: AuthorizationFlow
    store: FakeStore
    idp: Any
    upstream: UpstreamClient
    clock: list[float]
    groups: set[str] = field(default_factory=lambda: {"eng"})

    def request(self, **over: Any) -> dict[str, Any]:
        return {
            "redirect_uri": REDIRECT,
            "redirect_uri_provided_explicitly": True,
            "state": "client-state",
            "scopes": ["kh:read", "kh:write"],
            "code_challenge": "ch",
            "resource": "https://hub.example.test/mcp",
            **over,
        }

    async def begin(self, request: dict[str, Any] | None = None, nonce: str = "n1") -> str:
        """Store an upstream row as ``authorize`` would and let the fake IdP see the authorize request."""
        verifier, challenge = self.upstream.new_pkce()
        state = new_secret()
        url = await self.upstream.authorization_url(state, nonce, challenge)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as http:
            await http.get(url)
        expires = datetime.fromtimestamp(self.clock[0] + 600, UTC)
        await self.store.pending_put(
            token_hash(state), "upstream", "c1", request or self.request(), nonce, verifier, expires
        )
        return state

    app: Any = None


@pytest.fixture
async def env():
    app = make_fake_idp(ISSUER, CLIENT_ID, "s3cret")
    cfg = UpstreamConfig(
        ISSUER, CLIENT_ID, "s3cret", ("example.test",), (), "https://hub.example.test/oauth/callback"
    )
    clock = [1_000_000.0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        upstream = UpstreamClient(cfg, http, now=lambda: clock[0])
        store = FakeStore(clock)
        holder: dict[str, Any] = {}
        membership = SimpleNamespace(
            current=lambda: SimpleNamespace(
                audiences_for_email=lambda email, active: frozenset(g for g in holder["groups"] if active(g))
            )
        )
        config = SimpleNamespace(is_active=lambda g: True)
        settings = SimpleNamespace(redirect_allowlist_tuple=(REDIRECT, "http://localhost"))
        flow = AuthorizationFlow(store, upstream, settings, config, membership, now=lambda: clock[0])
        e = Env(flow, store, app.state.idp, upstream, clock)
        holder["groups"] = e.groups
        e.app = app
        yield e


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def assert_denied(url: str | None) -> None:
    assert url is not None and url.startswith(REDIRECT + "?")
    assert query(url) == {"error": "access_denied", "state": "client-state"}


# --- state problems ----------------------------------------------------------------------------------------


async def test_missing_unknown_used_and_expired_state(env: Env) -> None:
    with pytest.raises(FlowError):
        await env.flow.handle_callback(FIXED_CODE, None, None)
    with pytest.raises(FlowError):
        await env.flow.handle_callback(FIXED_CODE, "nope", None)
    state = await env.begin()
    await env.flow.handle_callback(FIXED_CODE, state, None)
    with pytest.raises(FlowError):
        await env.flow.handle_callback(FIXED_CODE, state, None)
    state = await env.begin()
    env.clock[0] += 601
    with pytest.raises(FlowError):
        await env.flow.handle_callback(FIXED_CODE, state, None)


async def test_state_and_ticket_are_not_interchangeable(env: Env) -> None:
    state = await env.begin()
    with pytest.raises(FlowError):
        await env.flow.consent_context(state)
    with pytest.raises(FlowError):
        await env.flow.decide_consent(state, "x", True)
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    with pytest.raises(FlowError):
        await env.flow.handle_callback(FIXED_CODE, out.consent_ticket, None)


# --- access_denied ends ------------------------------------------------------------------------------------


async def test_upstream_error_redirects_access_denied(env: Env) -> None:
    out = await env.flow.handle_callback(None, await env.begin(), "access_denied")
    assert_denied(out.redirect_url)
    assert out.consent_ticket is None


@pytest.mark.parametrize(
    "claims",
    [{"email_verified": False}, {"email": "mallory@evil.example", "hd": "evil.example"}],
    ids=["unverified", "foreign-domain"],
)
async def test_bad_identity_redirects_access_denied(env: Env, claims: dict[str, Any]) -> None:
    env.idp.claims.update(claims)
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    assert_denied(out.redirect_url)


async def test_wrong_nonce_redirects_access_denied(env: Env) -> None:
    env.idp.claims["nonce"] = "other"
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    assert_denied(out.redirect_url)


async def test_token_exchange_failure_redirects_access_denied(env: Env) -> None:
    out = await env.flow.handle_callback("wrong-code", await env.begin(), None)
    assert_denied(out.redirect_url)


async def test_no_group_redirects_access_denied(env: Env) -> None:
    env.groups.clear()
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    assert_denied(out.redirect_url)
    assert not env.store.codes


async def test_disallowed_redirect_uri_redirects_access_denied_or_fails(env: Env) -> None:
    state = await env.begin(env.request(redirect_uri="https://evil.example/cb"))
    with pytest.raises(FlowError):
        await env.flow.handle_callback(FIXED_CODE, state, None)
    assert not env.store.codes


# --- success and consent -----------------------------------------------------------------------------------


async def test_stored_consent_issues_code_at_once(env: Env) -> None:
    env.store.consents.add((EMAIL, "c1"))
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    assert out.consent_ticket is None and out.csrf is None
    q = query(out.redirect_url)
    assert q["state"] == "client-state"
    assert token_hash(q["code"]) in env.store.codes


async def test_no_consent_returns_ticket_and_csrf(env: Env) -> None:
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    assert out == CallbackOutcome(None, out.consent_ticket, out.csrf)
    assert out.consent_ticket and out.csrf and out.redirect_url is None
    row = env.store.pending[(token_hash(out.consent_ticket), "consent")][0]
    assert row.client_id == "c1" and row.nonce == "" and row.verifier == ""
    assert set(row.params) == {"email", "request", "csrf"}
    assert row.params["email"] == EMAIL and row.params["csrf"] == out.csrf


async def test_consent_context_fields(env: Env) -> None:
    env.store.clients["c1"] = {"client_name": "x" * 150}
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    ctx = await env.flow.consent_context(out.consent_ticket)
    assert ctx == ConsentContext("x" * 100, "claude.ai", "https://claude.ai", EMAIL, out.csrf)


async def test_consent_context_origin_keeps_port(env: Env) -> None:
    state = await env.begin(env.request(redirect_uri="http://localhost:54321/cb"))
    out = await env.flow.handle_callback(FIXED_CODE, state, None)
    ctx = await env.flow.consent_context(out.consent_ticket)
    assert (ctx.redirect_host, ctx.redirect_origin) == ("localhost", "http://localhost:54321")


async def test_allow_stores_consent_authorizes_and_issues_code(env: Env) -> None:
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    url = await env.flow.decide_consent(out.consent_ticket, out.csrf, True)
    q = query(url)
    assert q["state"] == "client-state" and token_hash(q["code"]) in env.store.codes
    assert (EMAIL, "c1") in env.store.consents and env.store.authorized == ["c1"]
    client_id, email, params, _ = env.store.codes[token_hash(q["code"])]
    assert (client_id, email) == ("c1", EMAIL) and params == env.request()


async def test_deny_redirects_access_denied(env: Env) -> None:
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    assert_denied(await env.flow.decide_consent(out.consent_ticket, out.csrf, False))
    assert not env.store.consents and not env.store.codes


async def test_wrong_csrf_used_and_expired_ticket(env: Env) -> None:
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    for bad in (None, "", "wrong"):
        with pytest.raises(FlowError):
            await env.flow.decide_consent(out.consent_ticket, bad, True)
    await env.flow.decide_consent(out.consent_ticket, out.csrf, True)
    with pytest.raises(FlowError):
        await env.flow.decide_consent(out.consent_ticket, out.csrf, True)
    env.store.consents.clear()
    out = await env.flow.handle_callback(FIXED_CODE, await env.begin(), None)
    assert out.consent_ticket
    env.clock[0] += 601
    with pytest.raises(FlowError):
        await env.flow.decide_consent(out.consent_ticket, out.csrf, True)
    assert len(env.store.codes) == 1
