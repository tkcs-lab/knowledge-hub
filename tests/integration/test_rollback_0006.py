"""deploy/rollback-0006.sql undoes migration 0006 and the migration can be applied again."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from knowledge_hub.db.migrate import migrate

pytestmark = pytest.mark.integration

DB = "kh_fresh"
SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "rollback-0006.sql"
NEW_TABLES = (
    "api_keys",
    "oauth_clients",
    "oauth_pending",
    "oauth_codes",
    "oauth_tokens",
    "oauth_consents",
    "directory_snapshot",
)


def test_rollback_restores_the_claim_objects_and_migrate_reapplies(fresh_db):
    dsn = fresh_db.dsn("kh_migrator", DB)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SCRIPT.read_text(encoding="utf-8"))
    with psycopg.connect(dsn, autocommit=True) as conn:
        for table in NEW_TABLES:
            assert conn.execute("SELECT to_regclass(%s)", (f"kh.{table}",)).fetchone() == (None,), table
        left = conn.execute(
            "SELECT proname FROM pg_proc WHERE pronamespace = 'kh'::regnamespace"
            " AND (proname LIKE 'oauth\\_%' OR proname LIKE 'api\\_key\\_%'"
            " OR proname LIKE 'directory\\_snapshot\\_%')"
        ).fetchall()
        assert left == []
        assert conn.execute("SELECT to_regclass('kh.claim_jti') IS NOT NULL").fetchone() == (True,)
        assert conn.execute(
            "SELECT has_function_privilege('kh_app', 'kh.claim_seen(text, timestamptz)', 'EXECUTE')"
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT has_function_privilege('kh_indexer', 'kh.claim_seen(text, timestamptz)', 'EXECUTE')"
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT has_table_privilege('kh_indexer', 'kh.claim_jti', 'DELETE')"
        ).fetchone() == (True,)
        assert conn.execute("SELECT has_table_privilege('kh_app', 'kh.claim_jti', 'SELECT')").fetchone() == (
            False,
        )
        policies = {
            r[0] for r in conn.execute("SELECT policyname FROM pg_policies WHERE tablename = 'claim_jti'")
        }
        assert policies == {"kh_migrator_all", "kh_indexer_select", "kh_indexer_delete"}
        assert conn.execute(
            "SELECT count(*) FROM kh.schema_migrations WHERE name = '0006_identity.sql'"
        ).fetchone() == (0,)
    with psycopg.connect(fresh_db.dsn("kh_app", DB), autocommit=True) as app:
        assert app.execute("SELECT kh.claim_seen('j1', now() + interval '1 minute')").fetchone() == (True,)
        assert app.execute("SELECT kh.claim_seen('j1', now() + interval '1 minute')").fetchone() == (False,)

    assert migrate(dsn) == ["0006_identity.sql"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM kh.schema_migrations WHERE name = '0006_identity.sql'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT to_regclass('kh.claim_jti'), to_regclass('kh.api_keys') IS NOT NULL"
        ).fetchone() == (None, True)
        assert conn.execute("SELECT to_regprocedure('kh.claim_seen(text, timestamptz)')").fetchone() == (
            None,
        )
