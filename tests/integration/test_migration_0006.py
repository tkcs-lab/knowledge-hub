"""Migration 0006: identity tables, the definer functions and their privileges."""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.integration

DB = "kh_fresh"
TABLES = (
    "api_keys",
    "oauth_clients",
    "oauth_pending",
    "oauth_codes",
    "oauth_tokens",
    "oauth_consents",
    "directory_snapshot",
)
APP, IDX, MIG, NONE = "kh_app", "kh_indexer", "kh_migrator", "kh_nobody"

# function (with argument types) -> roles that may execute it; the migrator owns every function
FUNCTIONS = {
    "api_key_get(text)": {APP, MIG},
    "api_key_touch(text)": {APP, MIG},
    "oauth_client_get(text)": {APP, MIG},
    "oauth_client_put(text, jsonb, integer)": {APP, MIG},
    "oauth_client_authorize(text)": {APP, MIG},
    "oauth_client_touch(text)": {APP, MIG},
    "oauth_pending_put(bytea, text, text, jsonb, text, text, timestamptz)": {APP, MIG},
    "oauth_pending_take(bytea, text)": {APP, MIG},
    "oauth_pending_peek(bytea, text)": {APP, MIG},
    "oauth_code_put(bytea, text, text, jsonb, timestamptz)": {APP, MIG},
    "oauth_code_take(bytea)": {APP, MIG},
    "oauth_token_put(bytea, text, uuid, timestamptz, text, text, text[], text, timestamptz)": {APP, MIG},
    "oauth_token_get(bytea, text)": {APP, MIG},
    "oauth_refresh_use(bytea, integer)": {APP, MIG},
    "oauth_token_revoke(bytea)": {APP, MIG},
    "oauth_family_revoke(uuid)": {APP, MIG},
    "oauth_consent_has(text, text)": {APP, MIG},
    "oauth_consent_put(text, text)": {APP, MIG},
    "directory_snapshot_get()": {APP, IDX, MIG},
    "directory_snapshot_put(jsonb)": {IDX, MIG},
    "oauth_purge()": {IDX, MIG},
}


@pytest.fixture
def world(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kh_nobody')"
            " THEN CREATE ROLE kh_nobody; END IF; END $$"
        )
    return fresh_db


def connect(world, role):
    return psycopg.connect(world.dsn(role, DB), autocommit=True)


def h(text: str) -> bytes:
    return text.encode().ljust(32, b"_")


def put_token(app, token, *, kind="refresh", family=None, expires="1 hour", email="a@x.org"):
    family = family or str(uuid.uuid4())
    app.execute(
        "SELECT kh.oauth_token_put(%s, %s, %s::uuid, now(), 'c1', %s, %s::text[], 'https://h/mcp',"
        " now() + %s::interval)",
        (h(token), kind, family, email, ["mcp"], expires),
    )
    return family


def test_tables_exist_with_forced_row_security(world):
    with connect(world, MIG) as conn:
        rows = conn.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE relnamespace = 'kh'::regnamespace AND relname = ANY(%s) ORDER BY relname",
            (list(TABLES),),
        ).fetchall()
        assert rows == [(t, True, True) for t in sorted(TABLES)]
        pols = conn.execute(
            "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'kh' AND tablename = ANY(%s)"
            " ORDER BY tablename",
            (list(TABLES),),
        ).fetchall()
        assert pols == [(t, "kh_migrator_all") for t in sorted(TABLES)]


def test_0006_function_privileges_match_matrix(world):
    with connect(world, MIG) as conn:
        for sig, allowed in FUNCTIONS.items():
            for role in (APP, IDX, MIG, NONE):
                got = conn.execute(
                    "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')", (role, f"kh.{sig}")
                ).fetchone()[0]
                assert got == (role in allowed), (sig, role)
        names = {
            r[0]
            for r in conn.execute(
                "SELECT p.proname FROM pg_proc p WHERE p.pronamespace = 'kh'::regnamespace"
                " AND (p.proname LIKE 'oauth\\_%' OR p.proname LIKE 'api\\_key\\_%'"
                " OR p.proname LIKE 'directory\\_snapshot\\_%')"
            )
        }
        assert names == {sig.split("(")[0] for sig in FUNCTIONS}


def test_0006_functions_are_definer_with_search_path(world):
    with connect(world, MIG) as conn:
        for sig in FUNCTIONS:
            row = conn.execute(
                "SELECT prosecdef, proconfig FROM pg_proc WHERE oid = %s::regprocedure", (f"kh.{sig}",)
            ).fetchone()
            assert row == (True, ["search_path=kh, pg_temp"]), sig


def test_claim_objects_are_gone(world):
    with connect(world, MIG) as conn:
        assert conn.execute(
            "SELECT to_regclass('kh.claim_jti'), to_regprocedure('kh.claim_seen(text, timestamptz)')"
        ).fetchone() == (None, None)


@pytest.mark.parametrize("role", [APP, IDX])
@pytest.mark.parametrize("table", TABLES)
def test_roles_have_no_table_access(world, role, table):
    with connect(world, role) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(f"SELECT * FROM kh.{table}")


def test_api_key_get_hides_expired_and_revoked_and_touch_throttles(world):
    with connect(world, MIG) as mig, connect(world, APP) as app:
        for kid, extra in (
            ("aaaaaaaaaa", ""),
            ("bbbbbbbbbb", ", revoked_at = now()"),
            ("cccccccccc", ", expires_at = now() - interval '1 second'"),
        ):
            mig.execute(
                "INSERT INTO kh.api_keys (id, secret_sha256, name, audience, tools, created_by)"
                " VALUES (%s, %s, 'k', 'all', ARRAY['search'], 'me')",
                (kid, h("s")),
            )
            if extra:
                mig.execute(f"UPDATE kh.api_keys SET id = id{extra} WHERE id = %s", (kid,))
        assert app.execute("SELECT * FROM kh.api_key_get('aaaaaaaaaa')").fetchall() == [
            (h("s"), "all", ["search"])
        ]
        assert app.execute("SELECT * FROM kh.api_key_get('bbbbbbbbbb')").fetchall() == []
        assert app.execute("SELECT * FROM kh.api_key_get('cccccccccc')").fetchall() == []

        def last():
            return mig.execute("SELECT last_used_at FROM kh.api_keys WHERE id = 'aaaaaaaaaa'").fetchone()[0]

        assert last() is None
        app.execute("SELECT kh.api_key_touch('aaaaaaaaaa')")
        first = last()
        assert first is not None
        app.execute("SELECT kh.api_key_touch('aaaaaaaaaa')")
        assert last() == first
        mig.execute("UPDATE kh.api_keys SET last_used_at = last_used_at - interval '61 seconds'")
        app.execute("SELECT kh.api_key_touch('aaaaaaaaaa')")
        assert last() > first


def test_client_put_get_cap_and_cleanup(world):
    with connect(world, MIG) as mig, connect(world, APP) as app:
        assert app.execute("SELECT kh.oauth_client_put('c1', '{\"n\": 1}', 2)").fetchone() == (True,)
        assert app.execute("SELECT kh.oauth_client_put('c2', '{}', 2)").fetchone() == (True,)
        assert app.execute("SELECT kh.oauth_client_put('c3', '{}', 2)").fetchone() == (False,)
        assert app.execute("SELECT kh.oauth_client_get('c1')").fetchone() == ({"n": 1},)
        assert app.execute("SELECT kh.oauth_client_get('c3')").fetchone() == (None,)
        # c1 authorized, c2 unauthorized and older than an hour: c2 is removed and the slot freed
        app.execute("SELECT kh.oauth_client_authorize('c1')")
        mig.execute("UPDATE kh.oauth_clients SET created_at = now() - interval '2 hours'")
        assert app.execute("SELECT kh.oauth_client_put('c3', '{}', 2)").fetchone() == (True,)
        assert [r[0] for r in mig.execute("SELECT client_id FROM kh.oauth_clients ORDER BY 1")] == [
            "c1",
            "c3",
        ]
        # touch moves last_used_at
        mig.execute("UPDATE kh.oauth_clients SET last_used_at = now() - interval '1 day'")
        app.execute("SELECT kh.oauth_client_touch('c1')")
        assert mig.execute(
            "SELECT last_used_at > now() - interval '1 minute' FROM kh.oauth_clients WHERE client_id = 'c1'"
        ).fetchone() == (True,)


def test_pending_take_peek_kind_and_expiry(world):
    exp = "now() + interval '10 minutes'"
    with connect(world, MIG) as mig, connect(world, APP) as app:
        put = "SELECT kh.oauth_pending_put(%s, %s, 'c1', '{\"p\": 1}', 'n', 'v', " + exp + ")"
        app.execute(put, (h("s1"), "upstream"))
        app.execute(put, (h("s2"), "consent"))
        # wrong kind returns nothing and keeps the row
        assert app.execute("SELECT * FROM kh.oauth_pending_take(%s, 'consent')", (h("s1"),)).fetchall() == []
        assert app.execute("SELECT * FROM kh.oauth_pending_take(%s, 'upstream')", (h("s2"),)).fetchall() == []
        # peek does not delete
        want = [("c1", {"p": 1}, "n", "v")]
        assert (
            app.execute("SELECT * FROM kh.oauth_pending_peek(%s, 'consent')", (h("s2"),)).fetchall() == want
        )
        assert (
            app.execute("SELECT * FROM kh.oauth_pending_peek(%s, 'consent')", (h("s2"),)).fetchall() == want
        )
        assert (
            app.execute("SELECT * FROM kh.oauth_pending_take(%s, 'upstream')", (h("s1"),)).fetchall() == want
        )
        assert app.execute("SELECT * FROM kh.oauth_pending_take(%s, 'upstream')", (h("s1"),)).fetchall() == []
        # expired rows are not returned and are deleted by the next put
        mig.execute("UPDATE kh.oauth_pending SET expires_at = now() - interval '1 second'")
        assert app.execute("SELECT * FROM kh.oauth_pending_take(%s, 'consent')", (h("s2"),)).fetchall() == []
        app.execute(put, (h("s3"), "upstream"))
        assert mig.execute("SELECT count(*) FROM kh.oauth_pending").fetchone() == (1,)


def test_code_is_single_use_and_expires(world):
    with connect(world, MIG) as mig, connect(world, APP) as app:
        app.execute(
            "SELECT kh.oauth_code_put(%s, 'c1', 'a@x.org', '{}', now() + interval '1 minute')", (h("k1"),)
        )
        row = app.execute(
            "SELECT client_id, email, params FROM kh.oauth_code_take(%s)", (h("k1"),)
        ).fetchall()
        assert row == [("c1", "a@x.org", {})]
        assert app.execute("SELECT * FROM kh.oauth_code_take(%s)", (h("k1"),)).fetchall() == []
        app.execute(
            "SELECT kh.oauth_code_put(%s, 'c1', 'a@x.org', '{}', now() + interval '1 minute')", (h("k2"),)
        )
        mig.execute("UPDATE kh.oauth_codes SET expires_at = now() - interval '1 second'")
        assert app.execute("SELECT * FROM kh.oauth_code_take(%s)", (h("k2"),)).fetchall() == []


def test_token_get_hides_expired_and_revoked_and_revoke_hits_family(world):
    with connect(world, MIG) as mig, connect(world, APP) as app:
        fam = put_token(app, "r1")
        put_token(app, "a1", kind="access", family=fam)
        put_token(app, "other", kind="access")
        assert app.execute(
            "SELECT email, scopes FROM kh.oauth_token_get(%s, 'access')", (h("a1"),)
        ).fetchall() == [("a@x.org", ["mcp"])]
        assert app.execute("SELECT * FROM kh.oauth_token_get(%s, 'refresh')", (h("a1"),)).fetchall() == []
        app.execute("SELECT kh.oauth_token_revoke(%s)", (h("a1"),))
        assert app.execute("SELECT * FROM kh.oauth_token_get(%s, 'access')", (h("a1"),)).fetchall() == []
        assert app.execute("SELECT * FROM kh.oauth_token_get(%s, 'refresh')", (h("r1"),)).fetchall() == []
        assert app.execute("SELECT * FROM kh.oauth_token_get(%s, 'access')", (h("other"),)).fetchall() != []
        mig.execute("UPDATE kh.oauth_tokens SET expires_at = now() - interval '1 second'")
        assert app.execute("SELECT * FROM kh.oauth_token_get(%s, 'access')", (h("other"),)).fetchall() == []


def test_family_revoke_revokes_every_token_of_the_family(world):
    with connect(world, MIG) as mig, connect(world, APP) as app:
        fam = put_token(app, "r1")
        put_token(app, "a1", kind="access", family=fam)
        keep = put_token(app, "r2")
        app.execute("SELECT kh.oauth_family_revoke(%s::uuid)", (fam,))
        rows = mig.execute("SELECT family_id::text, revoked_at IS NOT NULL FROM kh.oauth_tokens").fetchall()
        assert sorted(rows) == sorted([(fam, True), (fam, True), (keep, False)])


def test_refresh_use_states(world):
    with connect(world, APP) as app:
        put_token(app, "r1")
        assert app.execute("SELECT kh.oauth_refresh_use(%s, 60)", (h("r1"),)).fetchone() == ("ok",)
        assert app.execute("SELECT kh.oauth_refresh_use(%s, 60)", (h("r1"),)).fetchone() == ("reuse_grace",)
        assert app.execute("SELECT kh.oauth_refresh_use(%s, 0)", (h("r1"),)).fetchone() == ("reuse",)
        assert app.execute("SELECT kh.oauth_refresh_use(%s, 0)", (h("r1"),)).fetchone() == ("gone",)
        assert app.execute("SELECT kh.oauth_refresh_use(%s, 60)", (h("missing"),)).fetchone() == ("gone",)


def test_refresh_reuse_revokes_the_whole_family(world):
    with connect(world, MIG) as mig, connect(world, APP) as app:
        fam = put_token(app, "r1")
        put_token(app, "a1", kind="access", family=fam)
        other = put_token(app, "r2")
        app.execute("SELECT kh.oauth_refresh_use(%s, 0)", (h("r1"),))
        assert app.execute("SELECT kh.oauth_refresh_use(%s, 0)", (h("r1"),)).fetchone() == ("reuse",)
        rows = mig.execute("SELECT family_id::text, revoked_at IS NOT NULL FROM kh.oauth_tokens").fetchall()
        assert sorted(rows) == sorted([(fam, True), (fam, True), (other, False)])


def test_refresh_use_expired_is_gone(world):
    with connect(world, MIG) as mig, connect(world, APP) as app:
        put_token(app, "r1")
        mig.execute("UPDATE kh.oauth_tokens SET expires_at = now() - interval '1 second'")
        assert app.execute("SELECT kh.oauth_refresh_use(%s, 60)", (h("r1"),)).fetchone() == ("gone",)


async def test_refresh_use_concurrent_calls_yield_one_ok(world):
    with connect(world, APP) as app:
        put_token(app, "r1")

    async def use():
        async with await psycopg.AsyncConnection.connect(world.dsn(APP, DB), autocommit=True) as conn:
            cur = await conn.execute("SELECT kh.oauth_refresh_use(%s, 60)", (h("r1"),))
            return (await cur.fetchone())[0]

    assert sorted(await asyncio.gather(use(), use())) == ["ok", "reuse_grace"]


def test_consent(world):
    with connect(world, APP) as app:
        assert app.execute("SELECT kh.oauth_consent_has('a@x.org', 'c1')").fetchone() == (False,)
        app.execute("SELECT kh.oauth_consent_put('a@x.org', 'c1')")
        app.execute("SELECT kh.oauth_consent_put('a@x.org', 'c1')")
        assert app.execute("SELECT kh.oauth_consent_has('a@x.org', 'c1')").fetchone() == (True,)
        assert app.execute("SELECT kh.oauth_consent_has('a@x.org', 'c2')").fetchone() == (False,)


def test_snapshot_put_is_indexer_only(world):
    with connect(world, APP) as app, pytest.raises(errors.InsufficientPrivilege):
        app.execute("SELECT kh.directory_snapshot_put('{}')")
    with connect(world, IDX) as idx, connect(world, APP) as app, connect(world, MIG) as mig:
        assert idx.execute("SELECT * FROM kh.directory_snapshot_get()").fetchall() == []
        idx.execute("SELECT kh.directory_snapshot_put('{\"version\": 1}')")
        idx.execute("SELECT kh.directory_snapshot_put('{\"version\": 2}')")
        for conn in (idx, app, mig):
            rows = conn.execute(
                "SELECT data, fetched_at IS NOT NULL FROM kh.directory_snapshot_get()"
            ).fetchall()
            assert rows == [({"version": 2}, True)]


def test_oauth_purge_is_indexer_only(world):
    with connect(world, APP) as app, pytest.raises(errors.InsufficientPrivilege):
        app.execute("SELECT kh.oauth_purge()")


def test_oauth_purge_boundaries(world):
    with connect(world, MIG) as mig, connect(world, APP) as app, connect(world, IDX) as idx:
        put_token(app, "t23", kind="access")
        put_token(app, "t25", kind="access")
        mig.execute(
            "UPDATE kh.oauth_tokens SET expires_at = now() - interval '23 hours' WHERE token_sha256 = %s",
            (h("t23"),),
        )
        mig.execute(
            "UPDATE kh.oauth_tokens SET expires_at = now() - interval '25 hours' WHERE token_sha256 = %s",
            (h("t25"),),
        )
        for cid in ("used29", "used31", "new59", "new61"):
            app.execute("SELECT kh.oauth_client_put(%s, '{}', 100)", (cid,))
        for cid in ("used29", "used31"):
            app.execute("SELECT kh.oauth_client_authorize(%s)", (cid,))
        mig.execute(
            "UPDATE kh.oauth_clients SET last_used_at = now() - interval '29 days' WHERE client_id = 'used29'"
        )
        mig.execute(
            "UPDATE kh.oauth_clients SET last_used_at = now() - interval '31 days' WHERE client_id = 'used31'"
        )
        mig.execute(
            "UPDATE kh.oauth_clients SET created_at = now() - interval '59 minutes' WHERE client_id = 'new59'"
        )
        mig.execute(
            "UPDATE kh.oauth_clients SET created_at = now() - interval '61 minutes' WHERE client_id = 'new61'"
        )
        app.execute("SELECT kh.oauth_consent_put('a@x.org', 'used31')")
        app.execute(
            "SELECT kh.oauth_pending_put(%s, 'upstream', 'c', '{}', 'n', 'v', now() + interval '1 hour')",
            (h("p"),),
        )
        app.execute(
            "SELECT kh.oauth_code_put(%s, 'c', 'a@x.org', '{}', now() + interval '1 hour')", (h("c"),)
        )
        mig.execute("UPDATE kh.oauth_pending SET expires_at = now() - interval '1 second'")
        mig.execute("UPDATE kh.oauth_codes SET expires_at = now() - interval '1 second'")
        # t25, used31, new61, one pending row, one code row
        assert idx.execute("SELECT kh.oauth_purge()").fetchone() == (5,)
        assert [r[0] for r in mig.execute("SELECT client_id FROM kh.oauth_clients ORDER BY 1")] == [
            "new59",
            "used29",
        ]
        assert mig.execute("SELECT count(*) FROM kh.oauth_tokens").fetchone() == (1,)
        assert mig.execute("SELECT count(*) FROM kh.oauth_consents").fetchone() == (1,)
        assert idx.execute("SELECT kh.oauth_purge()").fetchone() == (0,)
