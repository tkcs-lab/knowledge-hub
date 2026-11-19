import hashlib
from importlib import resources

import psycopg
import pytest

from knowledge_hub.db import migrate as migrate_mod
from knowledge_hub.db.migrate import MigrationError, migrate

BASELINE = "0001_baseline.sql"
USAGE = "0002_usage_events.sql"
SUPERSEDE = "0003_supersede.sql"
TECH_TIER = "0004_tech_tier.sql"
MOVE = "0005_move_record.sql"
IDENTITY = "0006_identity.sql"


def _empty_db(cluster, name: str) -> str:
    with psycopg.connect(cluster.admin_dsn(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {name} OWNER kh_migrator")
    with psycopg.connect(cluster.admin_dsn(name), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")
    return cluster.dsn("kh_migrator", name)


def test_six_migrations_are_shipped():
    names = [n for n, _ in migrate_mod._migration_files()]
    assert names == [BASELINE, USAGE, SUPERSEDE, TECH_TIER, MOVE, IDENTITY]


def test_apply_twice_is_noop(fresh_db):
    dsn = fresh_db.dsn("kh_migrator", "kh_fresh")
    assert migrate(dsn) == []
    assert migrate(dsn) == []


def test_schema_migrations_lists_all_migrations(fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_migrator", "kh_fresh")) as conn:
        rows = conn.execute("SELECT name, sha256 FROM kh.schema_migrations ORDER BY name").fetchall()
    want = []
    for name in (BASELINE, USAGE, SUPERSEDE, TECH_TIER, MOVE, IDENTITY):
        text = resources.files("knowledge_hub").joinpath(f"migrations/{name}").read_text()
        want.append((name, hashlib.sha256(text.encode()).hexdigest()))
    assert rows == want


def test_modified_applied_file_raises(fresh_db, monkeypatch):
    real = migrate_mod._migration_files()
    changed = [(n, t + "\n-- edited") for n, t in real]
    monkeypatch.setattr(migrate_mod, "_migration_files", lambda: changed)
    with pytest.raises(MigrationError, match=BASELINE):
        migrate(fresh_db.dsn("kh_migrator", "kh_fresh"))


def test_missing_schema_migrations_means_nothing_applied(migrated_db):
    dsn = _empty_db(migrated_db, "kh_empty")
    assert migrate(dsn) == [BASELINE, USAGE, SUPERSEDE, TECH_TIER, MOVE, IDENTITY]


def test_foreign_schema_migrations_name_is_refused(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn("kh_fresh"), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.schema_migrations (name, sha256) VALUES ('0002_tables.sql', 'x')")
    with pytest.raises(MigrationError, match="database predates the baseline; a fresh install is required"):
        migrate(fresh_db.dsn("kh_migrator", "kh_fresh"))


def test_an_old_database_is_refused_before_anything_runs(migrated_db):
    dsn = _empty_db(migrated_db, "kh_old")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA kh")
        conn.execute(
            "CREATE TABLE kh.schema_migrations (name text PRIMARY KEY, sha256 text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.execute("INSERT INTO kh.schema_migrations (name, sha256) VALUES ('0001_schema_grants.sql', 'x')")
    with pytest.raises(MigrationError, match="fresh install is required"):
        migrate(dsn)
    with psycopg.connect(migrated_db.admin_dsn("kh_old")) as conn:
        assert conn.execute("SELECT to_regclass('kh.documents')").fetchone() == (None,)


def test_kh_app_has_no_superuser_or_bypassrls(fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_app", "kh_fresh")) as conn:
        row = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row == (False, False)
