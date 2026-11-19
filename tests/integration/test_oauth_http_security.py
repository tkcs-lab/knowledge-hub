"""HTTP-level refusals of the OAuth server and the public host (plan 58, 3.16)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from tests.support.oauth_app import CLAUDE_REDIRECT, PUBLIC_HOST, PUBLIC_URL, RESOURCE, pkce

EVIL = "https://evil.example/cb"


async def test_oauth_register_with_a_foreign_redirect_uri_is_refused(oauth_stack):
    resp = await oauth_stack.register(EVIL)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"
    assert oauth_stack.sql("SELECT count(*) FROM kh.oauth_clients") == [(0,)]


async def test_oauth_authorize_with_an_unregistered_redirect_uri_never_redirects(oauth_stack):
    s = oauth_stack
    client_id = await s.register_client()
    _, challenge = pkce()
    resp = await s.authorize(client_id, EVIL, challenge)
    assert resp.status_code == 400
    assert "location" not in resp.headers
    assert s.sql("SELECT count(*) FROM kh.oauth_pending") == [(0,)]


async def test_oauth_authorize_for_another_resource_is_refused(oauth_stack):
    s = oauth_stack
    client_id = await s.register_client()
    _, challenge = pkce()
    resp = await s.authorize(client_id, CLAUDE_REDIRECT, challenge, resource="https://other.example/mcp")
    query = parse_qs(urlsplit(resp.headers.get("location", "")).query)
    assert resp.status_code == 400 or "error" in query
    assert "code" not in query
    assert s.sql("SELECT count(*) FROM kh.oauth_pending") == [(0,)]


async def test_oauth_token_of_another_resource_is_401(oauth_stack):
    s = oauth_stack
    _, tokens = await s.login("alice@example.test")
    assert await s.search_ids(tokens["access_token"]) == [s.doc("all")]
    s.sql("UPDATE kh.oauth_tokens SET resource = 'https://other.example/mcp'")
    assert (await s.mcp_call(tokens["access_token"], "search", {"query": "chunk"})).status == 401


async def test_oauth_expired_access_token_is_401(oauth_stack):
    s = oauth_stack
    _, tokens = await s.login("alice@example.test")
    s.sql("UPDATE kh.oauth_tokens SET expires_at = now() - interval '1 minute' WHERE kind = 'access'")
    assert (await s.mcp_call(tokens["access_token"], "search", {"query": "chunk"})).status == 401


async def test_oauth_code_cannot_be_used_twice(oauth_stack):
    s = oauth_stack
    signed = await s.sign_in("alice@example.test")
    assert (await s.exchange(signed)).status_code == 200
    again = await s.exchange(signed)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


async def test_oauth_wrong_pkce_verifier_is_invalid_grant(oauth_stack):
    s = oauth_stack
    signed = await s.sign_in("alice@example.test")
    resp = await s.exchange(signed, verifier=pkce()[0])
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"


async def test_oauth_code_exchanged_by_another_client_is_invalid_grant(oauth_stack):
    s = oauth_stack
    signed = await s.sign_in("alice@example.test")
    other = await s.register_client()
    resp = await s.exchange(signed, client_id=other)
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"


async def test_oauth_redirect_uri_that_differs_at_the_token_endpoint_is_refused(oauth_stack):
    s = oauth_stack
    signed = await s.sign_in("alice@example.test")
    resp = await s.exchange(signed, redirect_uri="https://claude.ai/api/mcp/other")
    # The SDK answers a redirect_uri mismatch with invalid_request; either way no token is issued.
    assert resp.status_code == 400 and resp.json()["error"] in ("invalid_grant", "invalid_request")
    assert "access_token" not in resp.text
    assert s.sql("SELECT count(*) FROM kh.oauth_tokens") == [(0,)]


async def test_oauth_consent_post_without_the_cookie_is_400_and_issues_no_code(oauth_stack):
    s = oauth_stack
    client_id = await s.register_client()
    _, challenge = pkce()
    called = await s.upstream(await s.authorize(client_id, CLAUDE_REDIRECT, challenge), "alice@example.test")
    ticket = s.ticket_of(called.response)
    assert ticket and urlsplit(called.location).path == "/oauth/consent"
    page = await s.consent_page(ticket)
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]

    assert (await s.consent(None, csrf)).status_code == 400
    assert (await s.consent("not-the-ticket", csrf)).status_code == 400
    assert (await s.consent(ticket, "wrong-csrf")).status_code == 400
    assert s.sql("SELECT count(*) FROM kh.oauth_codes") == [(0,)]


async def test_oauth_consent_ticket_cookie_replayed_after_use_is_400(oauth_stack):
    s = oauth_stack
    client_id = await s.register_client()
    _, challenge = pkce()
    called = await s.upstream(await s.authorize(client_id, CLAUDE_REDIRECT, challenge), "alice@example.test")
    ticket = s.ticket_of(called.response)
    page = await s.consent_page(ticket)
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    done = await s.consent(ticket, csrf)
    assert done.status_code == 302 and "code=" in done.headers["location"]
    assert s.sql("SELECT count(*) FROM kh.oauth_codes") == [(1,)]

    assert (await s.consent(ticket, csrf)).status_code == 400
    assert (await s.consent_page(ticket)).status_code == 400
    assert s.sql("SELECT count(*) FROM kh.oauth_codes") == [(1,)]


@pytest.mark.parametrize(
    ("claims", "email"),
    [
        ({"email_verified": False}, "alice@example.test"),
        ({}, "alice@evil.example"),
    ],
    ids=["unverified-email", "foreign-domain"],
)
async def test_oauth_upstream_callback_with_a_bad_identity_gives_no_code(oauth_stack, claims, email):
    s = oauth_stack
    s.idp_state.claims.update(claims)
    signed = await s.sign_in(email)
    assert signed.code is None and signed.error == "access_denied"
    assert s.sql("SELECT count(*) FROM kh.oauth_codes") == [(0,)]
    assert s.sql("SELECT count(*) FROM kh.oauth_pending WHERE kind = 'consent'") == [(0,)]


async def test_oauth_upstream_callback_with_a_replayed_state_gives_no_code(oauth_stack):
    s = oauth_stack
    client_id = await s.register_client()
    _, challenge = pkce()
    first = await s.upstream(await s.authorize(client_id, CLAUDE_REDIRECT, challenge), "bob@example.test")
    assert first.response.status_code == 302
    replay = await s.callback(first.url)
    assert replay.response.status_code == 400
    assert "code=" not in replay.location
    assert s.sql("SELECT count(*) FROM kh.oauth_codes") == [(0,)]


@pytest.mark.parametrize("token", [None, "khk_nope", "kha_" + "A" * 43, "nonsense"])
async def test_oauth_every_401_from_mcp_names_the_resource_metadata(oauth_stack, token):
    resp = await oauth_stack.mcp_post(token, b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"' in challenge


async def test_oauth_metadata_names_the_resource(oauth_stack):
    resp = await oauth_stack.hub.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.status_code == 200
    assert resp.json()["resource"] == RESOURCE


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/webhooks/forgejo"), ("GET", "/metrics"), ("GET", "/healthz")],
)
async def test_oauth_internal_paths_are_404_on_the_public_host(oauth_stack, method, path):
    resp = await oauth_stack.hub.request(method, path, headers={"host": PUBLIC_HOST})
    assert resp.status_code == 404
    # The same path is not hidden on another host (the guard is by host, not by route).
    inner = await oauth_stack.hub.request(method, path, headers={"host": "kh.internal:8080"})
    assert inner.status_code != 404 or path == "/metrics"
