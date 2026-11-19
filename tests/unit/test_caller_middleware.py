"""``CallerMiddleware``: bearer resolution, dev identity, refusals and ``WWW-Authenticate`` (plan 3.10)."""

from __future__ import annotations

import json

import pytest

from knowledge_hub.auth.claims import Caller
from knowledge_hub.auth.middleware import CallerMiddleware
from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.metrics import METRICS, Metrics
from knowledge_hub.settings import Settings
from tests.support.bearer import TestBearerResolver, rpc_body

pytestmark = pytest.mark.unit

CONFIG = KhConfig(
    people={"alice": "alice@example.test"},
    groups={"all": GroupConfig(members=["alice"], approvers=["alice"])},
)
ALICE = Caller("u:alice@example.test", "claude_client", "alice@example.test", frozenset({"all"}), None)
CALL = rpc_body("tools/call", {"name": "search", "arguments": {"query": "x"}})


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **{"env": "test", **kw})


class Recorder:
    def __init__(self) -> None:
        self.scope = None
        self.body = b""

    async def __call__(self, scope, receive, send):
        self.scope = scope
        msg = await receive()
        self.body = msg["body"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def request(mw, *, method="POST", headers=(), body=CALL, chunks=None, path="/mcp"):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in headers],
    }
    queue = list(chunks) if chunks is not None else [body]
    state = {"consumed": 0}

    async def receive():
        state["consumed"] += 1
        chunk = queue.pop(0) if queue else b""
        return {"type": "http.request", "body": chunk, "more_body": bool(queue)}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)
    start = sent[0]
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, state["consumed"]


def make(public_url=None, resolver="default", **kw):
    app = Recorder()
    if resolver == "default":
        resolver = TestBearerResolver(CONFIG)
        resolver.register(ALICE)
    return CallerMiddleware(app, resolver, settings(**kw), config=CONFIG, public_url=public_url), app


async def test_caller_valid_bearer_reaches_the_app():
    mw, app = make()
    status, _, _ = await request(mw, headers=[("authorization", "Bearer test:" + ALICE.sub)])
    assert status == 200
    assert app.scope["state"]["kh_caller"] == ALICE
    assert app.scope["state"]["kh_call"].name == "search"
    assert app.scope["state"]["kh_call"].arguments == {"query": "x"}


@pytest.mark.parametrize(
    "header", ["Bearer nope", "Basic x", "Bearer", "Bearer ", "Bearer a b", "garbage", "bearer"]
)
async def test_caller_bad_authorization_is_bearer_unknown_never_dev(header):
    mw, app = make(dev_identity={"sub": "dev", "principal_type": "claude_client", "email": "alice@example.test"})
    status, headers, _ = await request(mw, headers=[("authorization", header)])
    assert status == 401 and app.scope is None
    assert "www-authenticate" in headers


async def test_caller_unauthenticated_get_and_delete_are_401_not_405():
    mw, _ = make()
    for method in ("GET", "DELETE"):
        status, headers, _ = await request(mw, method=method, body=b"")
        assert status == 401
        assert headers["www-authenticate"] == "Bearer"
        assert "allow" not in headers


async def test_caller_authenticated_get_and_delete_are_405():
    mw, _ = make()
    for method in ("GET", "DELETE"):
        status, headers, _ = await request(
            mw, method=method, headers=[("authorization", "Bearer test:" + ALICE.sub)], body=b""
        )
        assert status == 405 and headers["allow"] == "POST"


async def test_caller_no_credentials_is_missing_credentials():
    mw, app = make()
    status, _, _ = await request(mw)
    assert status == 401 and app.scope is None


async def test_caller_dev_identity_only_without_authorization_header():
    dev = {"sub": "dev", "principal_type": "claude_client", "email": "alice@example.test"}
    mw, app = make(dev_identity=dev)
    status, _, _ = await request(mw)
    assert status == 200
    assert app.scope["state"]["kh_caller"].sub == "dev"
    assert app.scope["state"]["kh_caller"].audiences == frozenset({"all"})
    assert app.scope["state"]["kh_caller"].tools is None
    status, _, _ = await request(mw, headers=[("authorization", "Bearer nope")])
    assert status == 401


async def test_caller_dev_workflow_identity_filters_inactive_groups():
    dev = {"sub": "wf", "principal_type": "n8n_workflow", "audiences": ["all", "gone"]}
    mw, app = make(dev_identity=dev)
    assert (await request(mw))[0] == 200
    assert app.scope["state"]["kh_caller"].audiences == frozenset({"all"})


def test_caller_dev_identity_refused_in_prod():
    dev = {"sub": "dev", "principal_type": "claude_client", "email": "alice@example.test"}
    with pytest.raises(ValueError, match="KH_DEV_IDENTITY"):
        make(dev_identity=dev, env="prod")


async def test_caller_www_authenticate_forms():
    mw, _ = make(public_url="https://hub.example.com")
    _, h, _ = await request(mw)
    assert h["www-authenticate"] == (
        'Bearer resource_metadata="https://hub.example.com/.well-known/oauth-protected-resource/mcp"'
    )
    _, h, _ = await request(mw, headers=[("authorization", "Bearer nope")])
    assert h["www-authenticate"] == (
        'Bearer resource_metadata="https://hub.example.com/.well-known/oauth-protected-resource/mcp",'
        ' error="invalid_token"'
    )
    mw, _ = make()
    _, h, _ = await request(mw, headers=[("authorization", "Bearer nope")])
    assert h["www-authenticate"] == 'Bearer error="invalid_token"'
    _, h, _ = await request(mw)
    assert h["www-authenticate"] == "Bearer"


async def test_caller_none_resolver_refuses_every_token():
    mw, app = make(resolver=None)
    status, _, _ = await request(mw, headers=[("authorization", "Bearer anything")])
    assert status == 401 and app.scope is None


async def test_caller_resolver_runs_before_the_body_is_read():
    mw, _ = make(max_body_bytes=10)
    status, _, consumed = await request(
        mw, headers=[("authorization", "Bearer nope")], chunks=[b"x" * 100]
    )
    assert status == 401 and consumed == 0


async def test_caller_oversized_body_with_valid_token_is_413():
    mw, _ = make(max_body_bytes=10)
    status, _, _ = await request(
        mw, headers=[("authorization", "Bearer test:" + ALICE.sub)], chunks=[b"x" * 100]
    )
    assert status == 413
    status, _, _ = await request(
        mw, headers=[("authorization", "Bearer test:" + ALICE.sub), ("content-length", "100")]
    )
    assert status == 413


async def test_caller_bad_json_body_is_401_bad_body():
    mw, app = make()
    status, _, _ = await request(
        mw, headers=[("authorization", "Bearer test:" + ALICE.sub)], body=b"{not json"
    )
    assert status == 401 and app.scope is None


async def test_caller_other_paths_pass_through():
    mw, app = make()
    status, _, _ = await request(mw, path="/healthz")
    assert status == 200 and app.scope is not None


async def test_caller_rejections_are_counted():
    mw, _ = make()
    before = METRICS.render()
    await request(mw, headers=[("authorization", "Bearer nope")])
    await request(mw)
    await request(mw, headers=[("authorization", "Bearer test:" + ALICE.sub)], body=b"[]")
    after = METRICS.render()
    for code in ("bearer_unknown", "missing_credentials", "bad_body"):
        assert f'kh_auth_rejections_total{{code="{code}"}}' in after
    assert before != after
    fresh = Metrics()
    fresh.auth_rejection("bearer_unknown")
    fresh.auth_rejection("whatever")
    text = fresh.render()
    assert 'kh_auth_rejections_total{code="bearer_unknown"} 1' in text
    assert 'kh_auth_rejections_total{code="other"} 1' in text
    assert json.dumps(text)
