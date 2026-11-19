"""The /metrics endpoint: token check, job states for the new job kinds, webhook counters, no leaks."""

from __future__ import annotations

import hashlib
import hmac
import json
import re

import httpx
import psycopg
import pytest

from knowledge_hub.app import build_app
from knowledge_hub.mcp.server import Deps
from knowledge_hub.metrics import METRICS
from tests.support.principals import alice_all

TOKEN = "metrics-secret-token"
SECRET = "hook-secret"


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
    hook = tmp_path / "hmac"
    hook.write_text(SECRET + "\n")
    return app_settings.model_copy(
        update={"cursor_key_file": key, "metrics_token_file": tok, "webhook_hmac_file": hook}
    )


@pytest.fixture
def deps(app_settings, kh_config, app_pool, indexer_pool):
    return Deps(settings=app_settings, config=kh_config, app_pool=app_pool, indexer_pool=indexer_pool)


async def scrape(http, token=TOKEN):
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return await http.get("/metrics", headers=headers)


async def hook(http, payload, event="push"):
    body = json.dumps(payload).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return await http.post(
        "/webhooks/forgejo", content=body, headers={"x-forgejo-event": event, "x-forgejo-signature": sig}
    )


async def test_scrape_lists_every_job_state(mcp_client):
    r = await scrape(mcp_client.http)
    assert r.status_code == 200
    for state in ("queued", "running", "done", "failed"):
        assert f'kh_index_jobs{{state="{state}"}} 0' in r.text


async def test_queued_jobs_of_both_kinds_are_counted(mcp_client, fresh_db):
    pushed = {"ref": "refs/heads/main", "repository": {"full_name": "kh/kb-all"}}
    with psycopg.connect(fresh_db.admin_dsn("kh_fresh"), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all')")
        conn.execute("INSERT INTO kh.index_jobs (kind, payload) VALUES ('embed_record', '{}')")
    assert (await hook(mcp_client.http, pushed)).status_code == 202
    text = (await scrape(mcp_client.http)).text
    assert 'kh_index_jobs{state="queued"} 2' in text
    assert 'kh_webhook_events_total{result="queued"} 1' in text


async def test_webhook_results_are_counted(mcp_client):
    other = {"ref": "refs/heads/main", "repository": {"full_name": "other/kb-all"}}
    assert (await hook(mcp_client.http, other)).status_code == 202
    bad = await mcp_client.http.post(
        "/webhooks/forgejo", content=b"{}", headers={"x-forgejo-signature": "0" * 64}
    )
    assert bad.status_code == 401
    text = (await scrape(mcp_client.http)).text
    assert 'kh_webhook_events_total{result="ignored"} 1' in text
    assert 'kh_webhook_events_total{result="bad_signature"} 1' in text


async def test_rejected_bearer_is_counted(mcp_client):
    res = await mcp_client.call(alice_all, "anything", {}, token="not-a-token")
    assert res.status == 401
    text = (await scrape(mcp_client.http)).text
    assert re.search(r'kh_auth_rejections_total\{code="[a-z_]+"\} 1', text)


async def test_tool_call_counters_are_rendered():
    METRICS.tool_call("search", "ok", 0.25)
    text = METRICS.render()
    assert 'kh_tool_calls_total{tool="search",outcome="ok"} 1' in text
    assert re.search(r'kh_tool_seconds_sum\{tool="search"\} 0\.25', text)


@pytest.mark.parametrize("token", [None, "wrong", "", TOKEN + "x"])
async def test_metrics_needs_the_token(mcp_client, token):
    assert (await scrape(mcp_client.http, token)).status_code == 401


async def test_non_bearer_scheme_is_refused(mcp_client):
    r = await mcp_client.http.get("/metrics", headers={"authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


async def test_metrics_text_leaks_nothing(mcp_client):
    pushed = {"ref": "refs/heads/main", "repository": {"full_name": "kh/kb-all"}}
    await hook(mcp_client.http, pushed)
    await mcp_client.call(alice_all, "anything", {}, token="junk")
    text = (await scrape(mcp_client.http)).text
    assert "@" not in text
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text)
    for secret in ("alice", "bob", "mgmt", "kb-all", "audience", TOKEN, SECRET):
        assert secret not in text.replace("kh_", "")


async def test_route_absent_without_token_file(app_settings, deps):
    settings = app_settings.model_copy(update={"metrics_token_file": None})
    app = build_app(settings, deps)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        assert (await http.get("/metrics", headers={"authorization": f"Bearer {TOKEN}"})).status_code == 404


async def test_empty_token_file_never_authenticates(app_settings, deps, tmp_path):
    empty = tmp_path / "empty.token"
    empty.write_text("\n")
    settings = app_settings.model_copy(update={"metrics_token_file": empty})
    app = build_app(settings, deps)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        for auth in ("Bearer ", "Bearer", ""):
            assert (await http.get("/metrics", headers={"authorization": auth})).status_code == 401


def test_label_values_come_from_fixed_sets():
    METRICS.auth_rejection("alice@example.org")
    METRICS.webhook_event("anything")
    METRICS.tool_call("t", "doc:123", 0.0)
    text = METRICS.render()
    assert 'code="other"' in text and 'result="other"' in text and 'outcome="error"' in text
    assert "alice" not in text and "doc:" not in text
