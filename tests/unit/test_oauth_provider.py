"""HubAuthProvider with a fake store and a fake upstream (no database)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.directory.model import Membership
from knowledge_hub.oauth.provider import HubAuthProvider
from knowledge_hub.oauth.store import PendingRow
from knowledge_hub.oauth.tokens import token_hash
from knowledge_hub.settings import Settings

pytestmark = pytest.mark.unit

PUBLIC = "https://hub.example.test"
RESOURCE = PUBLIC + "/mcp"


class FakeStore:
    def __init__(self, accept=True):
        self.accept = accept
        self.clients: dict = {}
        self.pending: list = []
        self.touched: list = []

    async def client_get(self, cid):
        return self.clients.get(cid)

    async def client_put(self, cid, info, max_clients):
        if not self.accept:
            return False
        self.clients[cid] = info
        return True

    async def pending_put(self, *args):
        self.pending.append(args)

    async def client_touch(self, cid):
        self.touched.append(cid)


class FakeUpstream:
    def new_pkce(self):
        return "verifier", "challenge"

    async def authorization_url(self, state, nonce, challenge):
        return f"https://idp.example.test/auth?state={state}&nonce={nonce}&cc={challenge}"


def make(store=None):
    settings = Settings(
        env="test",
        public_url=PUBLIC,
        oidc_issuer="https://idp.example.test",
        oidc_client_id="kh",
        oidc_client_secret_file="/dev/null",
        oidc_allowed_domains="x.org",
    )
    config = KhConfig(
        people={"sam": "sam@x.org"}, groups={"g": GroupConfig(members=["sam"], approvers=["sam"])}
    )
    membership = SimpleNamespace(current=lambda: Membership.from_config(config))
    store = store or FakeStore()
    return HubAuthProvider(store, FakeUpstream(), settings, config, membership), store


def client(uris=("https://claude.ai/api/mcp/auth_callback",), method="none", grants=None):
    return OAuthClientInformationFull(
        client_id="c1",
        redirect_uris=[AnyUrl(u) for u in uris],
        token_endpoint_auth_method=method,
        grant_types=grants or ["authorization_code", "refresh_token"],
        client_name="Claude",
    )


@pytest.mark.parametrize(
    "uri", ["https://claude.ai/api/mcp/auth_callback", "http://localhost:54321/cb", "http://127.0.0.1/x"]
)
async def test_register_accepts_allowlisted_redirects(uri):
    provider, store = make()
    await provider.register_client(client((uri,)))
    assert "c1" in store.clients
    assert (await provider.get_client("c1")).client_id == "c1"


@pytest.mark.parametrize("uri", ["https://evil.example/cb", "http://localhost.evil.example/cb"])
async def test_register_refuses_foreign_redirects(uri):
    provider, store = make()
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(client((uri,)))
    assert exc.value.error == "invalid_redirect_uri"
    assert store.clients == {}


@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic", None])
async def test_register_refuses_confidential_or_missing_auth_method(method):
    provider, _ = make()
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(client(method=method))
    assert (exc.value.error, exc.value.error_description) == (
        "invalid_client_metadata",
        "token_endpoint_auth_method must be none",
    )


async def test_register_refuses_wrong_grant_types_and_the_cap():
    provider, _ = make()
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(client(grants=["authorization_code"]))
    assert exc.value.error_description == "grant_types must be authorization_code and refresh_token"
    provider, _ = make(FakeStore(accept=False))
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(client())
    assert exc.value.error_description == "too many registered clients"


def params(**kw):
    base = {
        "state": "cs",
        "scopes": None,
        "code_challenge": "cc",
        "redirect_uri": AnyUrl("https://claude.ai/api/mcp/auth_callback"),
        "redirect_uri_provided_explicitly": True,
        "resource": None,
    }
    return AuthorizationParams(**{**base, **kw})


async def test_authorize_rejects_wrong_resource():
    provider, store = make()
    with pytest.raises(AuthorizeError) as exc:
        await provider.authorize(client(), params(resource="https://other.example/mcp"))
    assert exc.value.error == "invalid_target"
    assert exc.value.error_description == f"resource must be {RESOURCE}"
    assert store.pending == []


async def test_authorize_rechecks_the_allowlist():
    provider, store = make()
    with pytest.raises(AuthorizeError):
        await provider.authorize(client(), params(redirect_uri=AnyUrl("https://evil.example/cb")))
    assert store.pending == []


async def test_authorize_stores_pending_row_with_defaults():
    provider, store = make()
    url = await provider.authorize(client(), params())
    q = parse_qs(urlsplit(url).query)
    state, nonce = q["state"][0], q["nonce"][0]
    ((h, kind, cid, request, n, verifier, expires),) = store.pending
    assert h == token_hash(state) and kind == "upstream" and cid == "c1"
    assert n == nonce and verifier == "verifier" and isinstance(expires, datetime)
    assert request == {
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "redirect_uri_provided_explicitly": True,
        "state": "cs",
        "scopes": ["kh:read", "kh:write"],
        "code_challenge": "cc",
        "resource": RESOURCE,
    }
    assert expires.tzinfo is UTC


async def test_authorize_keeps_requested_scopes_and_resource():
    provider, store = make()
    await provider.authorize(client(), params(scopes=["kh:read"], resource=RESOURCE))
    assert store.pending[0][3]["scopes"] == ["kh:read"]
    assert store.pending[0][3]["resource"] == RESOURCE


async def test_exchange_code_for_another_client_is_refused():
    provider, _ = make()
    code = SimpleNamespace(client_id="other", subject="sam@x.org")
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client(), code)


def test_pending_row_shape():
    assert PendingRow("c", {}, "n", "v").verifier == "v"
