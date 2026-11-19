"""The real ``BearerResolver`` behind ``CallerMiddleware``, the host guard and the membership lifespan."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette

import knowledge_hub.auth.bearer as bearer_mod
from knowledge_hub.app import build_app
from knowledge_hub.auth.bearer import READ_TOOLS, WRITE_TOOLS, BearerResolver
from knowledge_hub.auth.claims import Call, Caller
from knowledge_hub.auth.hostguard import PublicHostGuard
from knowledge_hub.auth.middleware import CallerMiddleware
from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.mcp.server import Deps, build_mcp
from knowledge_hub.metrics import METRICS
from knowledge_hub.settings import Settings
from tests.support.bearer import rpc_body

pytestmark = pytest.mark.unit

RESOURCE = "https://hub.example.test/mcp"
CONFIG = KhConfig(
    people={"alice": "alice@example.test"},
    groups={
        "all": GroupConfig(members=["alice"], approvers=["alice"]),
        "ops": GroupConfig(members=["alice"], approvers=["alice"]),
    },
)
KEY_CALLER = Caller("key:abc", "n8n_workflow", None, frozenset({"all"}), "abc", frozenset({"search"}))


class FakeMembership:
    def __init__(self, groups):
        self.groups = frozenset(groups)
        self.refreshes = 0

    def current(self):
        return self

    def audiences_for_email(self, email, is_active):
        return self.groups if email == "alice@example.test" else frozenset()

    async def refresh(self):
        self.refreshes += 1


class FakeProvider:
    def __init__(self):
        self.tokens = {}

    async def load_access_token(self, token):
        return self.tokens.get(token)


def access(resource=RESOURCE, scopes=("kh:read", "kh:write"), subject="Alice@Example.test"):
    return SimpleNamespace(resource=resource, scopes=list(scopes), subject=subject)


@pytest.fixture
def parts(monkeypatch):
    async def fake_verify(pool, config, text):
        return KEY_CALLER if text == "khk_good" else None

    monkeypatch.setattr(bearer_mod, "verify_key", fake_verify)
    return FakeProvider(), FakeMembership({"all"})


class Recorder:
    def __init__(self):
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.scope = scope
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def call(resolver, token, body=None):
    app = Recorder()
    mw = CallerMiddleware(
        app, resolver, Settings(_env_file=None, env="test"), config=CONFIG, public_url="https://hub.example.test"
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    payload = body or rpc_body("tools/call", {"name": "search", "arguments": {}})
    sent = []

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(m):
        sent.append(m)

    await mw(scope, receive, send)
    return sent[0]["status"], {k.decode(): v.decode() for k, v in sent[0]["headers"]}, app


def resolver_for(provider, membership, resource=RESOURCE):
    return BearerResolver(None, CONFIG, provider, membership, resource)


async def test_valid_key_reaches_the_app(parts):
    provider, membership = parts
    status, _, app = await call(resolver_for(provider, membership), "khk_good")
    assert status == 200
    assert app.scope["state"]["kh_caller"] == KEY_CALLER


async def test_valid_oauth_token_reaches_the_app(parts):
    provider, membership = parts
    provider.tokens["kha_ok"] = access()
    status, _, app = await call(resolver_for(provider, membership), "kha_ok")
    assert status == 200
    assert app.scope["state"]["kh_caller"] == Caller(
        "u:alice@example.test",
        "claude_client",
        "alice@example.test",
        frozenset({"all"}),
        None,
        READ_TOOLS | WRITE_TOOLS,
    )


@pytest.mark.parametrize("token", ["khk_bad", "kha_missing", "other_x"])
async def test_unknown_token_is_401_invalid_token(parts, token):
    provider, membership = parts
    before = METRICS.render(None)
    status, headers, _ = await call(resolver_for(provider, membership), token)
    assert status == 401
    assert 'error="invalid_token"' in headers["www-authenticate"]
    assert 'bearer_unknown' in METRICS.render(None) and METRICS.render(None) != before


async def test_wrong_resource_is_401_bearer_resource(parts):
    provider, membership = parts
    provider.tokens["kha_other"] = access(resource="https://elsewhere.test/mcp")
    status, headers, _ = await call(resolver_for(provider, membership), "kha_other")
    assert status == 401
    assert 'error="invalid_token"' in headers["www-authenticate"]
    assert 'bearer_resource' in METRICS.render(None)
    assert await resolver_for(provider, membership).resolve("kha_other") == (None, "bearer_resource")


async def test_key_works_with_oauth_off(parts):
    _, membership = parts
    resolver = BearerResolver(None, CONFIG, None, membership, None)
    assert await resolver.resolve("khk_good") == (KEY_CALLER, "")
    assert await resolver.resolve("kha_anything") == (None, "bearer_unknown")


async def test_audiences_follow_the_membership_at_each_call(parts):
    provider, membership = parts
    provider.tokens["kha_ok"] = access()
    resolver = resolver_for(provider, membership)
    first, _ = await resolver.resolve("kha_ok")
    membership.groups = frozenset({"ops"})
    second, _ = await resolver.resolve("kha_ok")
    assert first.audiences == frozenset({"all"})
    assert second.audiences == frozenset({"ops"})


@pytest.mark.parametrize(
    ("scopes", "allowed", "refused"),
    [
        (["kh:read"], "search", "ingest_record"),
        (["kh:write"], "ingest_record", "search"),
    ],
)
async def test_scopes_limit_tools(parts, scopes, allowed, refused):
    provider, membership = parts
    provider.tokens["kha_s"] = access(scopes=scopes)
    caller, _ = await resolver_for(provider, membership).resolve("kha_s")
    assert allowed in caller.tools
    assert refused not in caller.tools


async def test_no_scope_means_no_tools(parts):
    provider, membership = parts
    provider.tokens["kha_s"] = access(scopes=[])
    caller, _ = await resolver_for(provider, membership).resolve("kha_s")
    assert caller.tools == frozenset()


async def test_scope_sets_match_registered_tools():
    settings = Settings(_env_file=None, env="test")
    deps = Deps(settings=settings, config=CONFIG, app_pool=None, indexer_pool=None)
    names = {t.name for t in await build_mcp(deps).list_tools()}
    assert READ_TOOLS | WRITE_TOOLS == names
    assert not READ_TOOLS & WRITE_TOOLS


async def test_refused_tool_is_never_called_and_dispatch_must_match():
    from knowledge_hub.mcp.errors import TOOL_NOT_ALLOWED, tool_guard

    ran = []
    deps = SimpleNamespace(ratelimiter=None)

    @tool_guard("search", deps)
    async def fn(call, **kwargs):
        ran.append(1)

    def ctx(caller, call):
        request = SimpleNamespace(scope={"state": {"kh_caller": caller, "kh_call": call}})
        return SimpleNamespace(request_context=SimpleNamespace(request=request))

    write_only = Caller("u:a", "claude_client", "a@x", frozenset({"all"}), None, WRITE_TOOLS)
    with pytest.raises(Exception) as err:  # noqa: B017
        await fn(ctx(write_only, Call("search", {})))
    assert TOOL_NOT_ALLOWED in str(err.value)
    reader = Caller("u:a", "claude_client", "a@x", frozenset({"all"}), None, READ_TOOLS)
    with pytest.raises(Exception, match="internal error"):  # noqa: B017
        await fn(ctx(reader, Call("usage", {})))  # dispatch differs from the body's call
    assert ran == []


# --- the host guard ---


async def host_status(guard, host, path):
    seen = []

    async def send(m):
        seen.append(m)

    scope = {"type": "http", "method": "GET", "path": path, "headers": [(b"host", host.encode())]}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await guard(scope, receive, send)
    return seen[0]["status"], b"".join(m.get("body", b"") for m in seen[1:])


async def inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.parametrize("path", ["/webhooks/forgejo", "/metrics", "/healthz", "/mcp/../x"])
async def test_public_host_guard_hides_private_paths(path):
    guard = PublicHostGuard(inner, "hub.example.test", {"/mcp", "/token"})
    assert await host_status(guard, "hub.example.test", path) == (404, b"")
    assert (await host_status(guard, "internal:8080", path))[0] == 200


PUBLIC = ["/mcp", "/token", "/.well-known/oauth-authorization-server", "/oauth/callback"]


@pytest.mark.parametrize("path", PUBLIC)
async def test_public_host_guard_passes_public_paths(path):
    guard = PublicHostGuard(inner, "hub.example.test", {"/mcp", "/token"})
    assert (await host_status(guard, "hub.example.test", path))[0] == 200


# --- the lifespan ---


def make_deps(membership):
    return Deps(
        settings=Settings(_env_file=None, env="test", directory_view_seconds=5),
        config=CONFIG,
        app_pool=None,
        indexer_pool=None,
        membership=membership,
    )


async def test_lifespan_refreshes_before_first_request_and_every_view_seconds():
    membership = FakeMembership({"all"})
    ticks = asyncio.Queue()
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        await ticks.get()

    deps = make_deps(membership)
    app = build_app(deps.settings, deps, sleep=sleep)
    assert isinstance(app, Starlette)
    async with app.router.lifespan_context(app):
        assert membership.refreshes == 1
        for n in range(1, 4):
            ticks.put_nowait(None)
            for _ in range(20):
                await asyncio.sleep(0)
            assert membership.refreshes == 1 + n
    assert set(waits) == {5}


async def test_lifespan_without_membership_starts():
    deps = make_deps(None)
    app = build_app(deps.settings, deps)
    async with app.router.lifespan_context(app):
        pass
