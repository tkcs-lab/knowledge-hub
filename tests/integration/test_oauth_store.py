"""OAuthStore against the migrated database, as ``kh_app``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from knowledge_hub.oauth.store import CodeRow, OAuthStore, PendingRow, TokenRow
from knowledge_hub.oauth.tokens import ACCESS_PREFIX, REFRESH_PREFIX, new_secret, new_token, token_hash

pytestmark = pytest.mark.integration

DB = "kh_fresh"


def soon(seconds=600):
    return datetime.now(UTC) + timedelta(seconds=seconds)


def migrator(world):
    return psycopg.connect(world.dsn("kh_migrator", DB), autocommit=True)


@pytest.fixture
def store(fresh_db, app_pool):
    return OAuthStore(app_pool)


def test_token_helpers():
    t = new_token(ACCESS_PREFIX)
    assert t.startswith("kha_") and len(t) == 4 + 43
    assert new_token(REFRESH_PREFIX).startswith("khr_")
    assert len(new_secret()) == 43 and new_secret() != new_secret()
    assert len(token_hash(t)) == 32


async def test_client_round_trip_and_cap(store, fresh_db):
    assert await store.client_get("c1") is None
    assert await store.client_put("c1", {"redirect_uris": ["http://localhost"]}, 2) is True
    assert await store.client_get("c1") == {"redirect_uris": ["http://localhost"]}
    assert await store.client_put("c2", {}, 2) is True
    assert await store.client_put("c3", {}, 2) is False
    assert await store.client_get("c3") is None
    await store.client_authorize("c1")
    await store.client_touch("c1")
    with migrator(fresh_db) as conn:
        row = conn.execute("SELECT authorized_at FROM kh.oauth_clients WHERE client_id = 'c1'").fetchone()
    assert row[0] is not None


async def test_pending_take_is_single_use_and_kind_scoped(store):
    h = token_hash(new_secret())
    await store.pending_put(h, "upstream", "c1", {"a": 1}, "n", "v", soon())
    assert await store.pending_take(h, "consent") is None
    assert await store.pending_take(h, "upstream") == PendingRow("c1", {"a": 1}, "n", "v")
    assert await store.pending_take(h, "upstream") is None


async def test_pending_peek_keeps_the_row(store):
    h = token_hash(new_secret())
    await store.pending_put(h, "consent", "c1", {}, "n", "v", soon())
    assert await store.pending_peek(h, "upstream") is None
    assert await store.pending_peek(h, "consent") == PendingRow("c1", {}, "n", "v")
    assert await store.pending_peek(h, "consent") is not None
    assert await store.pending_take(h, "consent") is not None


async def test_pending_expired_is_not_returned(store, fresh_db):
    h = token_hash(new_secret())
    await store.pending_put(h, "upstream", "c1", {}, "n", "v", soon())
    with migrator(fresh_db) as conn:
        conn.execute("UPDATE kh.oauth_pending SET expires_at = now() - interval '1 minute'")
    assert await store.pending_peek(h, "upstream") is None
    assert await store.pending_take(h, "upstream") is None


async def test_code_take_is_single_use(store, fresh_db):
    h = token_hash(new_secret())
    exp = soon()
    await store.code_put(h, "c1", "a@x.org", {"p": "q"}, exp)
    row = await store.code_take(h)
    assert isinstance(row, CodeRow)
    assert (row.client_id, row.email, row.params) == ("c1", "a@x.org", {"p": "q"})
    assert abs((row.expires_at - exp).total_seconds()) < 1
    assert await store.code_take(h) is None
    h2 = token_hash(new_secret())
    await store.code_put(h2, "c1", "a@x.org", {}, soon())
    with migrator(fresh_db) as conn:
        conn.execute("UPDATE kh.oauth_codes SET expires_at = now() - interval '1 minute'")
    assert await store.code_take(h2) is None


async def put(store, token, kind="access", family=None, expires=None):
    family = family or uuid.uuid4()
    await store.token_put(
        token_hash(token),
        kind,
        family,
        datetime.now(UTC),
        "c1",
        "a@x.org",
        ["mcp"],
        "https://h/mcp",
        expires or soon(3600),
    )
    return family


async def test_token_round_trip_and_kind(store):
    fam = await put(store, "t1")
    row = await store.token_get(token_hash("t1"), "access")
    assert isinstance(row, TokenRow)
    assert (row.family_id, row.client_id, row.email, row.scopes, row.resource, row.used_at) == (
        fam,
        "c1",
        "a@x.org",
        ["mcp"],
        "https://h/mcp",
        None,
    )
    assert await store.token_get(token_hash("t1"), "refresh") is None
    assert await store.token_get(token_hash("nope"), "access") is None


async def test_token_get_none_after_expiry_and_revoke(store, fresh_db):
    await put(store, "t1")
    await put(store, "t2")
    with migrator(fresh_db) as conn:
        conn.execute(
            "UPDATE kh.oauth_tokens SET expires_at = now() - interval '1 minute' WHERE token_sha256 = %s",
            (token_hash("t1"),),
        )
    assert await store.token_get(token_hash("t1"), "access") is None
    await store.token_revoke(token_hash("t2"))
    assert await store.token_get(token_hash("t2"), "access") is None


async def test_refresh_use_four_outcomes(store, fresh_db):
    fam = await put(store, "r1", kind="refresh")
    other = await put(store, "r9", kind="refresh")
    await put(store, "a1", family=fam)
    assert await store.refresh_use(token_hash("r1"), 10) == "ok"
    assert (await store.token_get(token_hash("r1"), "refresh")).used_at is not None
    assert await store.refresh_use(token_hash("r1"), 10) == "reuse_grace"
    assert await store.token_get(token_hash("a1"), "access") is not None
    with migrator(fresh_db) as conn:
        conn.execute(
            "UPDATE kh.oauth_tokens SET used_at = now() - interval '1 hour' WHERE token_sha256 = %s",
            (token_hash("r1"),),
        )
    assert await store.refresh_use(token_hash("r1"), 10) == "reuse"
    assert await store.token_get(token_hash("a1"), "access") is None
    assert await store.refresh_use(token_hash("r1"), 10) == "gone"
    assert await store.refresh_use(token_hash("missing"), 10) == "gone"
    assert await store.token_get(token_hash("r9"), "refresh") is not None and other


async def test_family_revoke_hides_access_tokens(store):
    fam = await put(store, "a1")
    await put(store, "a2", family=fam)
    await put(store, "r1", kind="refresh", family=fam)
    keep = await put(store, "a3")
    await store.family_revoke(fam)
    for t, k in (("a1", "access"), ("a2", "access"), ("r1", "refresh")):
        assert await store.token_get(token_hash(t), k) is None
    assert await store.token_get(token_hash("a3"), "access") is not None and keep


async def test_consent_round_trip(store):
    assert await store.consent_has("a@x.org", "c1") is False
    await store.consent_put("a@x.org", "c1")
    await store.consent_put("a@x.org", "c1")
    assert await store.consent_has("a@x.org", "c1") is True
    assert await store.consent_has("b@x.org", "c1") is False
