import json
import logging
from pathlib import Path

import httpx
import pytest

from knowledge_hub.auth.claims import (
    Caller,
    ClaimMiddleware,
    InMemoryReplayCache,
    KhAuthError,
    _build_caller,
    caller_from_scope,
    resolve_audiences,
)
from knowledge_hub.auth.pf_claim_verify import CLAIM_HEADER, ClaimVerifier, request_hash
from knowledge_hub.settings import DevIdentity, Settings
from tests.support.claims import (
    AUDIENCE,
    ISSUER,
    call_body,
    mint_claim,
    rpc_body,
    static_jwks,
    verifier_for_tests,
)
from tests.support.principals import KH_CONFIG

pytestmark = pytest.mark.unit

VECTORS = Path(__file__).resolve().parents[1] / "data" / "pf_req_test_vectors.json"
ARGS = {"query": "policy", "limit": 5}


class Recorder:
    """Inner ASGI app: records what the middleware handed over and answers 200."""

    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        message = await receive()
        self.calls.append({"state": dict(scope.get("state", {})), "body": message["body"]})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def make_settings(**kw):
    kw.setdefault("env", "test")
    return Settings(**kw)


def make_client(settings=None, replay=None):
    inner = Recorder()
    app = ClaimMiddleware(
        inner,
        verifier_for_tests(),
        settings or make_settings(),
        replay or InMemoryReplayCache(),
        config=KH_CONFIG,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub")
    return client, inner


def call_token(**kw):
    kw.setdefault("sub", "person-1")
    kw.setdefault("principal_type", "claude_client")
    kw.setdefault("audiences", ["all"])
    kw.setdefault("tool", "search")
    kw.setdefault("arguments", ARGS)
    return mint_claim(**kw)


async def post(client, body, token=None, path="/mcp"):
    headers = {"content-type": "application/json"}
    if token is not None:
        headers[CLAIM_HEADER] = token
    return await client.post(path, content=body, headers=headers)


def test_vectors_reproduce():
    data = json.loads(VECTORS.read_text())
    assert len(data["vectors"]) == 6
    for v in data["vectors"]:
        assert request_hash(v["tool"], v["arguments"]) == v["req"], v["name"]


async def test_valid_claim_passes_and_sets_state():
    client, inner = make_client()
    body = call_body("search", ARGS)
    r = await post(client, body, call_token(email="Alice@Example.test"))
    assert r.status_code == 200 and r.content == b"ok"
    (seen,) = inner.calls
    assert seen["body"] == body
    caller = seen["state"]["kh_caller"]
    assert caller == Caller("person-1", "claude_client", "alice@example.test", frozenset({"all"}), None)
    call = seen["state"]["kh_call"]
    assert call.name == "search" and call.arguments == ARGS
    assert caller_from_scope({"state": seen["state"]}) is caller


async def test_non_call_request_has_no_call_and_no_audiences():
    client, inner = make_client()
    r = await post(client, rpc_body("initialize", {}), mint_claim("person-1", "claude_client"))
    assert r.status_code == 200
    assert inner.calls[0]["state"]["kh_call"] is None
    assert inner.calls[0]["state"]["kh_caller"].audiences == frozenset()


async def test_other_paths_pass_through():
    client, inner = make_client()
    r = await post(client, b"{}", path="/healthz")
    assert r.status_code == 200 and "kh_caller" not in inner.calls[0]["state"]


def test_caller_from_scope_without_caller():
    with pytest.raises(KhAuthError):
        caller_from_scope({})
    with pytest.raises(KhAuthError):
        caller_from_scope({"state": {}})


async def refused(client, body, token, status=401):
    r = await post(client, body, token)
    assert r.status_code == status and r.content == b""


async def test_missing_header():
    client, inner = make_client()
    await refused(client, call_body("search", ARGS), None)
    assert not inner.calls


async def test_expired_claim():
    client, _ = make_client()
    await refused(client, call_body("search", ARGS), call_token(now=1_000_000.0))


async def test_wrong_req():
    client, _ = make_client()
    await refused(client, call_body("search", {"query": "other"}), call_token())


async def test_batch_body():
    client, _ = make_client()
    batch = json.dumps([json.loads(call_body("search", ARGS))]).encode()
    await refused(client, batch, call_token())


async def test_human_refused():
    client, _ = make_client()
    await refused(client, rpc_body("initialize", {}), mint_claim("h", "human"))


async def test_service_on_tools_call_refused_but_not_on_listing():
    client, _ = make_client()
    await refused(client, call_body("search", ARGS), call_token(sub="privacyfence", principal_type="service"))
    r = await post(client, rpc_body("tools/list"), mint_claim("privacyfence", "service"))
    assert r.status_code == 200


async def test_replayed_jti():
    client, _ = make_client()
    token = call_token()
    body = call_body("search", ARGS)
    assert (await post(client, body, token)).status_code == 200
    await refused(client, body, token)


async def test_oversize_body():
    client, inner = make_client(make_settings(max_body_bytes=200))
    big = call_body("search", {"query": "x" * 500})
    await refused(client, big, call_token(arguments={"query": "x" * 500}), status=413)
    assert not inner.calls


@pytest.mark.parametrize("name", ["All", "a b", "", "-x", "x" * 64, "al,l"])
async def test_bad_audience_name(name):
    client, _ = make_client()
    await refused(client, call_body("search", ARGS), call_token(audiences=[name]))


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_get_and_delete_are_405_with_valid_claim(method):
    client, inner = make_client()
    r = await client.request(method, "/mcp", headers={CLAIM_HEADER: mint_claim("p", "claude_client")})
    assert r.status_code == 405 and r.content == b""
    assert not inner.calls
    r = await client.request(method, "/mcp")
    assert r.status_code == 401 and r.content == b""


async def test_n8n_email_dropped():
    client, inner = make_client()
    token = call_token(
        sub="n8n-token:t1", principal_type="n8n_workflow", audiences=["all"], email="wf@example.org"
    )
    assert (await post(client, call_body("search", ARGS), token)).status_code == 200
    caller = inner.calls[0]["state"]["kh_caller"]
    assert caller.email is None and caller.token_id == "t1" and caller.audiences == {"all"}


IDENT = DevIdentity(sub="dev", principal_type="claude_client", audiences=["hr"], email="Multi@Example.test")


async def test_dev_identity_without_header(caplog):
    client, inner = make_client(make_settings(env="dev", dev_identity=IDENT))
    with caplog.at_level(logging.WARNING):
        r = await post(client, call_body("search", ARGS), None)
    assert r.status_code == 200
    state = inner.calls[0]["state"]
    assert state["kh_caller"].sub == "dev" and state["kh_caller"].email == "multi@example.test"
    assert state["kh_caller"].audiences == {"all", "mgmt"}
    assert state["kh_call"].name == "search"
    assert "dev identity" in caplog.text


async def test_dev_identity_does_not_bypass_a_bad_header():
    client, _ = make_client(make_settings(env="dev", dev_identity=IDENT))
    await refused(client, call_body("search", ARGS), "not-a-jwt")


def test_prod_refuses_dev_identity():
    with pytest.raises(ValueError, match="refusing to start"):
        ClaimMiddleware(
            Recorder(),
            verifier_for_tests(),
            Settings.model_construct(env="prod", dev_identity=IDENT, max_body_bytes=1000),
            None,
            config=KH_CONFIG,
        )


def test_http_jwks_url_with_other_host_than_issuer_is_accepted():
    fetched: list[str] = []
    url = "http://pf.internal.example:8765/upstream-mcp/jwks"

    def fake(u):
        fetched.append(u)
        return json.dumps(static_jwks())

    verifier = ClaimVerifier(issuer=ISSUER, audience=AUDIENCE, jwks_url=url, fetch=fake, replay_cache=None)
    token = mint_claim("person", "claude_client", ["all"], tool="search", arguments=ARGS)
    principal = verifier.verify(token, body=call_body("search", ARGS))
    assert principal.sub == "person"
    assert fetched == [url]


@pytest.mark.parametrize(
    ("ptype", "email", "expected"),
    [
        ("claude_client", "Dev@Example.org", "dev@example.org"),
        ("n8n_workflow", "wf@example.org", None),
        ("service", "svc@example.org", None),
    ],
)
def test_email_kept_only_for_claude_client(ptype, email, expected):
    assert _build_caller("sub", ptype, email, ["all"], None, None, config=KH_CONFIG).email == expected


async def test_claim_audiences_are_ignored_for_claude_client():
    client, inner = make_client()
    token = call_token(audiences=["hr"], email="alice@example.test")
    assert (await post(client, call_body("search", ARGS), token)).status_code == 200
    assert inner.calls[0]["state"]["kh_caller"].audiences == {"all"}


@pytest.mark.parametrize("email", ["nobody@example.test", None])
def test_unknown_or_missing_email_has_no_audiences(email):
    caller = _build_caller("s", "claude_client", email, ["all"], None, None, config=KH_CONFIG)
    assert caller.audiences == frozenset()


def test_upper_case_email_matches():
    caller = _build_caller("s", "claude_client", "ALICE@EXAMPLE.TEST", [], None, None, config=KH_CONFIG)
    assert caller.audiences == {"all"} and caller.email == "alice@example.test"


def test_n8n_keeps_only_configured_groups():
    caller = _build_caller("s", "n8n_workflow", None, ["all", "nope"], "t", None, config=KH_CONFIG)
    assert caller.audiences == {"all"}


def test_resolve_audiences_other_types_are_empty():
    assert resolve_audiences(KH_CONFIG, "service", "alice@example.test", ["all"]) == frozenset()


def test_archived_group_is_not_held_by_person_or_workflow():
    config = KH_CONFIG.model_copy(deep=True)
    config.groups["hr"].archived = True
    assert resolve_audiences(config, "claude_client", "carol@example.test", []) == frozenset()
    assert resolve_audiences(config, "claude_client", "alice@example.test", []) == {"all"}
    assert resolve_audiences(config, "n8n_workflow", None, ["all", "hr"]) == {"all"}


async def test_bad_audience_name_still_refused_for_claude_client():
    client, _ = make_client()
    await refused(
        client, call_body("search", ARGS), call_token(audiences=["Bad Name"], email="alice@example.test")
    )


def test_dev_identity_of_each_type_resolves():
    n8n = DevIdentity(
        sub="wf", principal_type="n8n_workflow", audiences=["mgmt", "nope"], email="alice@example.test"
    )
    caller = ClaimMiddleware(
        Recorder(), verifier_for_tests(), make_settings(env="dev", dev_identity=n8n), None, config=KH_CONFIG
    )._dev_caller(None)
    assert caller.audiences == {"mgmt"} and caller.email is None
    person = ClaimMiddleware(
        Recorder(), verifier_for_tests(), make_settings(env="dev", dev_identity=IDENT), None, config=KH_CONFIG
    )._dev_caller(None)
    assert person.audiences == {"all", "mgmt"}
