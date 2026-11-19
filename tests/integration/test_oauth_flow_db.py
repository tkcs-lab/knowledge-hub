"""AuthorizationFlow code and ticket handling against the migrated database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest

from knowledge_hub.oauth.flow import AuthorizationFlow, FlowError
from knowledge_hub.oauth.store import OAuthStore
from knowledge_hub.oauth.tokens import new_secret, token_hash

pytestmark = pytest.mark.integration

DB = "kh_fresh"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
EMAIL = "alice@example.test"
REQUEST = {
    "redirect_uri": REDIRECT,
    "redirect_uri_provided_explicitly": True,
    "state": "st",
    "scopes": ["kh:read"],
    "code_challenge": "ch",
    "resource": "https://hub.example.test/mcp",
}


@pytest.fixture
async def store(fresh_db, app_pool):
    s = OAuthStore(app_pool)
    await s.client_put("c1", {"client_name": "Claude", "redirect_uris": [REDIRECT]}, 10)
    return s


@pytest.fixture
def flow(store):
    membership = SimpleNamespace(
        current=lambda: SimpleNamespace(audiences_for_email=lambda e, a: frozenset({"eng"}))
    )
    settings = SimpleNamespace(redirect_allowlist_tuple=(REDIRECT,))
    return AuthorizationFlow(store, None, settings, SimpleNamespace(is_active=lambda g: True), membership)


def migrator(world):
    return psycopg.connect(world.dsn("kh_migrator", DB), autocommit=True)


async def test_issue_code_is_hashed_single_use_and_expires(flow, store, fresh_db):
    url = await flow.issue_code(REQUEST, EMAIL, "c1")
    code = parse_qs(urlsplit(url).query)["code"][0]
    assert parse_qs(urlsplit(url).query)["state"] == ["st"]
    with migrator(fresh_db) as conn:
        dump = str(conn.execute("SELECT * FROM kh.oauth_codes").fetchall())
        assert code not in dump
        row = await store.code_take(token_hash(code))
        assert row is not None and (row.client_id, row.email, row.params) == ("c1", EMAIL, REQUEST)
        assert 55 <= (row.expires_at - datetime.now(UTC)).total_seconds() <= 61
        assert await store.code_take(token_hash(code)) is None

        url = await flow.issue_code(REQUEST, EMAIL, "c1")
        code = parse_qs(urlsplit(url).query)["code"][0]
        conn.execute("UPDATE kh.oauth_codes SET expires_at = %s", (datetime.now(UTC) - timedelta(seconds=1),))
    assert await store.code_take(token_hash(code)) is None


async def test_consent_ticket_is_single_use(flow, store):
    ticket, csrf = new_secret(), new_secret()
    await store.pending_put(
        token_hash(ticket),
        "consent",
        "c1",
        {"email": EMAIL, "request": REQUEST, "csrf": csrf},
        "",
        "",
        datetime.now(UTC) + timedelta(seconds=600),
    )
    ctx = await flow.consent_context(ticket)
    assert (ctx.client_name, ctx.email, ctx.csrf) == ("Claude", EMAIL, csrf)
    url = await flow.decide_consent(ticket, csrf, True)
    assert "code=" in url
    with pytest.raises(FlowError):
        await flow.decide_consent(ticket, csrf, True)
    with pytest.raises(FlowError):
        await flow.consent_context(ticket)
