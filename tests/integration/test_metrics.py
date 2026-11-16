"""Rate limits and the /metrics endpoint (plan 3.2)."""

from __future__ import annotations

import re

import httpx
import pytest

from knowledge_hub.app import build_app
from knowledge_hub.config import Config
from knowledge_hub.mcp.server import Deps
from knowledge_hub.metrics import METRICS
from knowledge_hub.ratelimit import RateLimiter
from tests.support.claims import verifier_for_tests
from tests.support.principals import alice_all, bob_mgmt

TOKEN = "metrics-secret-token"


@pytest.fixture(autouse=True)
def fresh_metrics():
    METRICS.reset()
    yield
    METRICS.reset()


@pytest.fixture
def app_settings(app_settings, tmp_path):
    key = tmp_path / "cursor.key"
    key.write_bytes(b"k" * 32)
    tok = tmp_path / "metrics.token"
    tok.write_text(TOKEN + "\n")
    return app_settings.model_copy(update={"cursor_key_file": key, "metrics_token_file": tok})


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool):
    return Deps(
        settings=app_settings,
        config=Config(),
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        ratelimiter=RateLimiter(3),
    )


async def scrape(http, token=TOKEN):
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return await http.get("/metrics", headers=headers)


async def test_tool_call_counts_and_scrape(mcp_client):
    res = await mcp_client.call(alice_all, "list_stale", {})
    assert not res.is_error, res
    r = await scrape(mcp_client.http)
    assert r.status_code == 200
    text = r.text
    assert 'kh_tool_calls_total{tool="list_stale",outcome="ok"} 1' in text
    assert 'kh_tool_seconds_count{tool="list_stale"} 1' in text
    assert re.search(r'kh_tool_seconds_sum\{tool="list_stale"\} [0-9.]+', text)
    assert 'kh_index_jobs{state="queued"} 0' in text


async def test_rejected_claim_is_counted(mcp_client):
    res = await mcp_client.call(alice_all, "list_stale", {}, token="not-a-claim")
    assert res.status == 401
    text = (await scrape(mcp_client.http)).text
    assert re.search(r'kh_claim_rejections_total\{code="[a-z_]+"\} 1', text)


async def test_rate_limit_text_and_counter(mcp_client):
    for _ in range(3):
        assert not (await mcp_client.call(alice_all, "list_stale", {})).is_error
    res = await mcp_client.call(alice_all, "list_stale", {})
    assert res.is_error and re.search(r"rate limit exceeded; retry in \d+ seconds", res.text)
    assert not (await mcp_client.call(bob_mgmt, "list_stale", {})).is_error  # per-sub bucket
    text = (await scrape(mcp_client.http)).text
    assert 'kh_tool_calls_total{tool="list_stale",outcome="rate_limited"} 1' in text


@pytest.mark.parametrize("token", [None, "wrong", "", TOKEN + "x"])
async def test_metrics_needs_the_token(mcp_client, token):
    assert (await scrape(mcp_client.http, token)).status_code == 401


async def test_non_bearer_scheme_is_refused(mcp_client):
    r = await mcp_client.http.get("/metrics", headers={"authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


async def test_metrics_text_leaks_nothing(mcp_client):
    for who in (alice_all, bob_mgmt):
        await mcp_client.call(who, "list_stale", {})
    await mcp_client.call(alice_all, "get_doc", {"id": "doc:00000000-0000-4000-8000-000000000001"})
    await mcp_client.call(alice_all, "list_stale", {}, token="junk")
    text = (await scrape(mcp_client.http)).text
    assert "@" not in text
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text)
    for secret in ("alice", "bob", "mgmt", "hr", "contracts", "audience", TOKEN):
        assert secret not in text.replace("kh_", "")


async def test_route_absent_without_token_file(app_settings, deps):
    settings = app_settings.model_copy(update={"metrics_token_file": None})
    app = build_app(settings, deps, verifier_for_tests())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        assert (await http.get("/metrics", headers={"authorization": f"Bearer {TOKEN}"})).status_code == 404


async def test_empty_token_file_never_authenticates(app_settings, deps, tmp_path):
    empty = tmp_path / "empty.token"
    empty.write_text("\n")
    settings = app_settings.model_copy(update={"metrics_token_file": empty})
    app = build_app(settings, deps, verifier_for_tests())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        for auth in ("Bearer ", "Bearer", ""):
            assert (await http.get("/metrics", headers={"authorization": auth})).status_code == 401


def test_label_values_come_from_fixed_sets():
    METRICS.claim_rejection("alice@example.org")
    METRICS.webhook_event("anything")
    METRICS.tool_call("t", "doc:123", 0.0)
    text = METRICS.render()
    assert 'code="other"' in text and 'result="other"' in text and 'outcome="error"' in text
    assert "alice" not in text and "doc:" not in text


async def test_agent_write_failure_counter_is_rendered(mcp_client):
    from knowledge_hub.agent.guards import record_write_failure
    from knowledge_hub.forgejo.client import ForgejoError, ForgejoRefused

    record_write_failure("write_note", ForgejoRefused("no"))
    record_write_failure("write_note", ForgejoError("boom"))
    record_write_failure("write_note", ForgejoError("boom"))
    text = (await scrape(mcp_client.http)).text
    assert 'kh_agent_write_failures_total{tool="write_note",reason="refused"} 1' in text
    assert 'kh_agent_write_failures_total{tool="write_note",reason="forgejo_error"} 2' in text
    assert "# TYPE kh_agent_write_failures_total counter" in text
    METRICS.reset()
    assert "kh_agent_write_failures_total{" not in METRICS.render()
