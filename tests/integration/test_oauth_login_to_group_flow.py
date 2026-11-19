"""Login to group, end to end: the real app, the fake IdP and a fake directory (plan 58, 3.16)."""

from __future__ import annotations

import logging
import re

from knowledge_hub.mcp.errors import TOOL_NOT_ALLOWED
from tests.support.oauth_app import CLAUDE_REDIRECT


async def test_oauth_login_search_sees_only_the_directory_group(oauth_stack):
    s = oauth_stack
    signed = await s.sign_in("alice@example.test")
    assert signed.consent_shown
    assert signed.code and signed.error is None
    assert signed.query["state"] == [signed.state]
    tokens = await s.tokens(signed)
    assert tokens["access_token"].startswith("kha_") and tokens["refresh_token"].startswith("khr_")
    assert await s.search_ids(tokens["access_token"]) == [s.doc("all")]


async def test_oauth_removed_from_the_directory_loses_access_and_refresh(oauth_stack):
    s = oauth_stack
    signed, tokens = await s.login("alice@example.test")
    assert await s.search_ids(tokens["access_token"]) == [s.doc("all")]

    s.provider.remove("all-g", "alice@example.test")
    await s.sync()

    assert await s.search_ids(tokens["access_token"]) == []
    resp = await s.refresh(signed.client_id, tokens["refresh_token"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_oauth_person_in_static_people_only_signs_in_and_reads(oauth_stack):
    s = oauth_stack
    _, tokens = await s.login("sam@example.test")
    assert await s.search_ids(tokens["access_token"]) == [s.doc("all")]


async def test_oauth_person_disabled_in_the_directory_gets_access_denied(oauth_stack):
    s = oauth_stack
    signed = await s.sign_in("dana@example.test")
    assert signed.error == "access_denied"
    assert signed.code is None
    assert signed.location.startswith(CLAUDE_REDIRECT)
    assert not signed.consent_shown
    assert s.sql("SELECT count(*) FROM kh.oauth_codes") == [(0,)]


async def test_oauth_second_sign_in_of_the_same_person_and_client_skips_consent(oauth_stack):
    s = oauth_stack
    first = await s.sign_in("bob@example.test")
    assert first.consent_shown and first.code
    second = await s.sign_in("bob@example.test", client_id=first.client_id)
    assert not second.consent_shown
    assert second.code and second.code != first.code
    tokens = await s.tokens(second)
    assert await s.search_ids(tokens["access_token"]) == [s.doc("mgmt")]


async def test_oauth_hub_key_reads_only_its_group_and_its_tools(oauth_stack):
    s = oauth_stack
    key = await s.create_key("mgmt", ("search",))
    assert await s.search_ids(key) == [s.doc("mgmt")]
    denied = await s.mcp_call(key, "get_doc", {"id": s.doc("mgmt")})
    assert denied.is_error and denied.text == TOOL_NOT_ALLOWED
    other = await s.mcp_call(key, "list_labels")
    assert other.is_error and other.text == TOOL_NOT_ALLOWED


async def test_oauth_off_hub_key_still_works(keys_stack):
    s = keys_stack
    key = await s.create_key("hr", ("search", "get_doc"))
    assert await s.search_ids(key) == [s.doc("hr")]
    got = await s.mcp_call(key, "get_doc", {"id": s.doc("all")})
    assert got.is_error and got.text.endswith("not found")
    assert (await s.hub.get("/authorize")).status_code == 404


async def test_oauth_revoked_key_and_revoked_family_give_401(oauth_stack):
    s = oauth_stack
    key = await s.create_key("all", ("search",))
    assert await s.search_ids(key) == [s.doc("all")]
    s.revoke_key(key)
    assert (await s.mcp_call(key, "search", {"query": "chunk"})).status == 401

    signed, tokens = await s.login("alice@example.test")
    assert await s.search_ids(tokens["access_token"]) == [s.doc("all")]
    revoked = await s.revoke(signed.client_id, tokens["refresh_token"])
    assert revoked.status_code == 200, revoked.text
    assert (await s.mcp_call(tokens["access_token"], "search", {"query": "chunk"})).status == 401
    assert (await s.refresh(signed.client_id, tokens["refresh_token"])).json()["error"] == "invalid_grant"


async def test_raw_tokens_never_logged(oauth_stack, caplog):
    s = oauth_stack
    caplog.set_level(logging.DEBUG)
    key = await s.create_key("all", ("search",))
    signed, tokens = await s.login("alice@example.test")
    s.revoke_key(key)
    s.sql("UPDATE kh.oauth_tokens SET resource = 'https://other.example/mcp' WHERE kind = 'access'")
    refused = [
        key,  # revoked
        key[:-3] + "AAA",  # wrong secret
        tokens["access_token"],  # token for another resource
        "kha_" + "A" * 43,  # unknown access token
        "khr_" + "A" * 43,
        tokens["refresh_token"],  # a refresh token is not a bearer
    ]
    for bearer in refused:
        assert (await s.mcp_call(bearer, "search", {"query": "chunk"})).status == 401
    assert (await s.exchange(signed)).status_code == 400  # the code is spent
    assert (await s.refresh(signed.client_id, "khr_" + "B" * 43)).status_code == 400
    for secret in (key, tokens["access_token"], tokens["refresh_token"], signed.verifier):
        assert secret not in caplog.text
    assert not re.search(r"kh[ark]_[A-Za-z0-9_.-]{6,}", caplog.text)
