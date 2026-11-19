"""HubAuthProvider against the migrated database (plan 58, 3.9)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.directory.model import Membership
from knowledge_hub.oauth.provider import HubAuthProvider
from knowledge_hub.oauth.store import OAuthStore
from knowledge_hub.oauth.tokens import new_secret, token_hash
from knowledge_hub.settings import Settings

pytestmark = pytest.mark.integration

DB = "kh_fresh"
PUBLIC = "https://hub.example.test"
RESOURCE = PUBLIC + "/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


class NoUpstream:
    pass


def migrator(world):
    return psycopg.connect(world.dsn("kh_migrator", DB), autocommit=True)


def make_config(members=("sam",)):
    return KhConfig(
        people={"sam": "sam@x.org"}, groups={"g": GroupConfig(members=list(members), approvers=["sam"])}
    )


@pytest.fixture
def box(fresh_db, app_pool):
    config = make_config()
    holder = SimpleNamespace(membership=Membership.from_config(config))
    holder.view = SimpleNamespace(current=lambda: holder.membership)
    store = OAuthStore(app_pool)
    holder.store = store
    holder.provider = HubAuthProvider(
        store,
        NoUpstream(),
        Settings(
            env="test",
            public_url=PUBLIC,
            oidc_issuer="https://idp.example.test",
            oidc_client_id="kh",
            oidc_client_secret_file="/dev/null",
            oidc_allowed_domains="x.org",
        ),
        config,
        holder.view,
    )
    return holder


def cl(cid):
    return OAuthClientInformationFull(
        client_id=cid,
        redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
    )


async def issue(box, client, email="sam@x.org"):
    """Store a code the way the flow does, then exchange it; return (raw code, tokens)."""
    code = new_secret()
    params = {
        "redirect_uri": REDIRECT,
        "redirect_uri_provided_explicitly": True,
        "state": "s",
        "scopes": ["kh:read", "kh:write"],
        "code_challenge": "cc",
        "resource": RESOURCE,
    }
    await box.store.code_put(
        token_hash(code), client.client_id, email, params, datetime.now(UTC) + timedelta(seconds=60)
    )
    return code


async def tokens(box, client):
    code = await issue(box, client)
    loaded = await box.provider.load_authorization_code(client, code)
    assert loaded is not None and loaded.subject == "sam@x.org"
    return await box.provider.exchange_authorization_code(client, loaded)


async def test_code_is_single_use(box):
    c = cl("a")
    code = await issue(box, c)
    assert await box.provider.load_authorization_code(c, code) is not None
    assert await box.provider.load_authorization_code(c, code) is None


async def test_tokens_are_stored_hashed(box, fresh_db):
    t = await tokens(box, cl("a"))
    with migrator(fresh_db) as conn:
        rows = conn.execute("SELECT * FROM kh.oauth_tokens").fetchall()
        assert len(rows) == 2
        for raw in (t.access_token, t.refresh_token):
            for row in rows:
                assert all(raw.encode() != v and raw != v for v in row if isinstance(v, (bytes, str)))
        assert {r[0] for r in rows} == {token_hash(t.access_token), token_hash(t.refresh_token)}
    access = await box.provider.load_access_token(t.access_token)
    assert access.subject == "sam@x.org" and access.resource == RESOURCE


async def test_rotation_grace_and_reuse(box, fresh_db):
    c = cl("a")
    t = await tokens(box, c)
    r1 = await box.provider.load_refresh_token(c, t.refresh_token)
    t2 = await box.provider.exchange_refresh_token(c, r1, r1.scopes)
    assert t2.refresh_token != t.refresh_token
    # a second use inside the grace: invalid_grant, family untouched
    with pytest.raises(TokenError) as exc:
        await box.provider.exchange_refresh_token(c, r1, r1.scopes)
    assert exc.value.error == "invalid_grant"
    assert await box.provider.load_access_token(t2.access_token) is not None
    # reuse after the grace: the whole family is revoked
    with migrator(fresh_db) as conn:
        conn.execute(
            "UPDATE kh.oauth_tokens SET used_at = now() - interval '1 hour' WHERE token_sha256 = %s",
            (token_hash(t.refresh_token),),
        )
    with pytest.raises(TokenError):
        await box.provider.exchange_refresh_token(c, r1, r1.scopes)
    assert await box.provider.load_access_token(t2.access_token) is None
    assert await box.provider.load_refresh_token(c, t2.refresh_token) is None


async def test_concurrent_refresh_mints_one_pair(box, fresh_db):
    c = cl("a")
    t = await tokens(box, c)
    r = await box.provider.load_refresh_token(c, t.refresh_token)
    results = await asyncio.gather(
        box.provider.exchange_refresh_token(c, r, r.scopes),
        box.provider.exchange_refresh_token(c, r, r.scopes),
        return_exceptions=True,
    )
    assert sum(isinstance(x, TokenError) for x in results) == 1
    with migrator(fresh_db) as conn:
        n = conn.execute("SELECT count(*) FROM kh.oauth_tokens WHERE kind = 'access'").fetchone()[0]
    assert n == 2  # the first pair and exactly one new one


async def test_refresh_for_person_without_group_revokes_family(box):
    c = cl("a")
    t = await tokens(box, c)
    r = await box.provider.load_refresh_token(c, t.refresh_token)
    box.membership = Membership.from_config(make_config(members=()).model_copy(update={"people": {}}))
    with pytest.raises(TokenError) as exc:
        await box.provider.exchange_refresh_token(c, r, r.scopes)
    assert exc.value.error == "invalid_grant"
    assert await box.provider.load_access_token(t.access_token) is None


async def test_refresh_past_family_ttl_is_refused(box, fresh_db):
    c = cl("a")
    t = await tokens(box, c)
    r = await box.provider.load_refresh_token(c, t.refresh_token)
    with migrator(fresh_db) as conn:
        conn.execute("UPDATE kh.oauth_tokens SET family_started_at = now() - interval '31 days'")
    with pytest.raises(TokenError) as exc:
        await box.provider.exchange_refresh_token(c, r, r.scopes)
    assert exc.value.error == "invalid_grant"
    assert await box.provider.load_access_token(t.access_token) is None


async def test_load_access_token_none_when_expired(box, fresh_db):
    t = await tokens(box, cl("a"))
    with migrator(fresh_db) as conn:
        conn.execute("UPDATE kh.oauth_tokens SET expires_at = now() - interval '1 minute'")
    assert await box.provider.load_access_token(t.access_token) is None


async def test_load_access_token_none_when_revoked(box, fresh_db):
    t = await tokens(box, cl("a"))
    with migrator(fresh_db) as conn:
        conn.execute("UPDATE kh.oauth_tokens SET revoked_at = now()")
    assert await box.provider.load_access_token(t.access_token) is None


async def test_other_client_cannot_use_code_or_refresh_token(box):
    a, b = cl("a"), cl("b")
    code = await issue(box, a)
    assert await box.provider.load_authorization_code(b, code) is None
    t = await tokens(box, a)
    assert await box.provider.load_refresh_token(b, t.refresh_token) is None
    r = await box.provider.load_refresh_token(a, t.refresh_token)
    with pytest.raises(TokenError):
        await box.provider.exchange_refresh_token(b, r, r.scopes)


async def test_revoke_access_token_hides_refresh_token(box):
    c = cl("a")
    t = await tokens(box, c)
    access = await box.provider.load_access_token(t.access_token)
    await box.provider.revoke_token(access)
    assert await box.provider.load_access_token(t.access_token) is None
    assert await box.provider.load_refresh_token(c, t.refresh_token) is None
