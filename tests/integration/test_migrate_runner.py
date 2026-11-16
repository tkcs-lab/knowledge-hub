import hashlib
from importlib import resources

import psycopg
import pytest

from knowledge_hub.db import migrate as migrate_mod
from knowledge_hub.db.migrate import MigrationError, migrate


def test_apply_twice_is_noop(fresh_db):
    dsn = fresh_db.dsn("kh_migrator", "kh_fresh")
    assert migrate(dsn) == []
    assert migrate(dsn) == []


def test_schema_migrations_lists_0001(fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_migrator", "kh_fresh")) as conn:
        rows = conn.execute("SELECT name, sha256 FROM kh.schema_migrations ORDER BY name").fetchall()
    text = resources.files("knowledge_hub").joinpath("migrations/0001_schema_grants.sql").read_text()
    assert rows[0] == ("0001_schema_grants.sql", hashlib.sha256(text.encode()).hexdigest())


def test_modified_applied_file_raises(fresh_db, monkeypatch):
    real = migrate_mod._migration_files()
    changed = [(n, t + "\n-- edited") if n.startswith("0001") else (n, t) for n, t in real]
    monkeypatch.setattr(migrate_mod, "_migration_files", lambda: changed)
    with pytest.raises(MigrationError, match="0001_schema_grants.sql"):
        migrate(fresh_db.dsn("kh_migrator", "kh_fresh"))


def test_missing_schema_migrations_means_nothing_applied(migrated_db):
    with psycopg.connect(migrated_db.admin_dsn(), autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS kh_empty WITH (FORCE)")
        conn.execute("CREATE DATABASE kh_empty OWNER kh_migrator")
    with psycopg.connect(migrated_db.admin_dsn("kh_empty"), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")
    assert migrate(migrated_db.dsn("kh_migrator", "kh_empty"))[0] == "0001_schema_grants.sql"


def test_kh_app_has_no_superuser_or_bypassrls(fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_app", "kh_fresh")) as conn:
        row = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row == (False, False)


def test_stop_after_applies_only_files_up_to_it(migrated_db):
    with psycopg.connect(migrated_db.admin_dsn(), autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS kh_stop WITH (FORCE)")
        conn.execute("CREATE DATABASE kh_stop OWNER kh_migrator")
    with psycopg.connect(migrated_db.admin_dsn("kh_stop"), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")
    dsn = migrated_db.dsn("kh_migrator", "kh_stop")
    done = migrate(dsn, stop_after="0006_strict_visibility.sql")
    assert [n[:4] for n in done] == ["0001", "0002", "0003", "0004", "0005", "0006"]
    assert migrate(dsn, stop_after="0007_any_of_audiences.sql") == ["0007_any_of_audiences.sql"]
    assert migrate(dsn) == ["0008_merge_verdicts.sql"]
