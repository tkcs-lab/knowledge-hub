"""Migration runner: numbered ``.sql`` files, one transaction each, checksums recorded."""

from __future__ import annotations

import hashlib
import sys
from importlib import resources

import psycopg

from knowledge_hub.settings import load_settings


class MigrationError(RuntimeError):
    """A migration cannot be applied or an applied file changed."""


def _migration_files() -> list[tuple[str, str]]:
    root = resources.files("knowledge_hub").joinpath("migrations")
    files = [p for p in root.iterdir() if p.name.endswith(".sql")]
    return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(files, key=lambda p: p.name)]


def _applied(conn: psycopg.Connection) -> dict[str, str]:
    exists = conn.execute("SELECT to_regclass('kh.schema_migrations') IS NOT NULL").fetchone()
    if not exists or not exists[0]:
        return {}
    return {name: sha for name, sha in conn.execute("SELECT name, sha256 FROM kh.schema_migrations")}


def migrate(dsn: str) -> list[str]:
    """Apply pending migrations; return the names applied in this run."""
    files = _migration_files()
    applied_now: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        applied = _applied(conn)
        for name, text in files:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if name in applied:
                if applied[name] != digest:
                    raise MigrationError(f"migration {name} was modified after it was applied")
                continue
            try:
                with conn.transaction():
                    conn.execute(text)  # type: ignore[arg-type]
                    conn.execute(
                        "INSERT INTO kh.schema_migrations (name, sha256) VALUES (%s, %s)",
                        (name, digest),
                    )
            except psycopg.Error as exc:
                raise MigrationError(f"migration {name} failed: {exc}") from exc
            applied_now.append(name)
    return applied_now


def migrate_main() -> int:
    """Entry point for ``kh migrate``."""
    settings = load_settings()
    try:
        done = migrate(settings.dsn("migrator"))
    except MigrationError as exc:
        print(exc, file=sys.stderr)
        return 1
    for name in done:
        print(f"applied {name}")
    return 0
