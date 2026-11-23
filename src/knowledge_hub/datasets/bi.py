"""Read-only database logins for BI tools, one generated role per use (plan 72, 3.9)."""

from __future__ import annotations

import re
import secrets

import psycopg
from psycopg import sql

from knowledge_hub.datasets.model import DatasetError

BI_GROUP_ROLE = "kh_bi"
ROLE_RE = re.compile(r"^kh_bi_[a-z2-7]{10}$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def _terminate(conn: psycopg.Connection, role: str) -> None:
    conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (role,))


def _check_role(migrator_dsn: str, role: str) -> None:
    if not ROLE_RE.fullmatch(role):
        raise DatasetError(f"{role} is not a BI role")
    with psycopg.connect(migrator_dsn) as conn:
        if conn.execute("SELECT 1 FROM kh.bi_roles WHERE role_name = %s", (role,)).fetchone() is None:
            raise DatasetError(f"{role} is not a BI role")


def create_bi_role(admin_dsn: str, migrator_dsn: str, group: str, by: str) -> tuple[str, str]:
    """Create a login for ``group`` and map it; returns ``(role, password)``."""
    with psycopg.connect(migrator_dsn) as conn:
        found = conn.execute("SELECT 1 FROM kh.groups WHERE name = %s", (group,)).fetchone()
    if found is None:
        raise DatasetError(f"{group} is not an active group")
    role = "kh_bi_" + "".join(secrets.choice(_ALPHABET) for _ in range(10))
    password = secrets.token_urlsafe(32)
    ident = sql.Identifier(role)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
                " CONNECTION LIMIT 5 PASSWORD {} IN ROLE {}"
            ).format(ident, sql.Literal(password), sql.Identifier(BI_GROUP_ROLE))
        )
        admin.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(ident))
        admin.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '30s'").format(ident))
        try:
            with psycopg.connect(migrator_dsn) as conn:
                conn.execute(
                    "INSERT INTO kh.bi_roles (role_name, audience, created_by) VALUES (%s, %s, %s)",
                    (role, group, by),
                )
        except psycopg.Error:
            admin.execute(sql.SQL("DROP ROLE {}").format(ident))
            raise
    return role, password


def rotate_bi_role(admin_dsn: str, migrator_dsn: str, role: str) -> str:
    """Set a new password for ``role``, ending its sessions; returns the password."""
    _check_role(migrator_dsn, role)
    password = secrets.token_urlsafe(32)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        _terminate(admin, role)
        admin.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
        )
    return password


def drop_bi_role(admin_dsn: str, migrator_dsn: str, role: str) -> None:
    """End the sessions of ``role``, drop it and delete its mapping."""
    _check_role(migrator_dsn, role)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        _terminate(admin, role)
        # a restore into a new cluster can leave the mapping without its role
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    with psycopg.connect(migrator_dsn) as conn:
        conn.execute("DELETE FROM kh.bi_roles WHERE role_name = %s", (role,))


def list_bi_roles(migrator_dsn: str) -> list[dict]:
    with psycopg.connect(migrator_dsn) as conn:
        rows = conn.execute(
            "SELECT role_name, audience, created_at, created_by FROM kh.bi_roles"
            " ORDER BY created_at, role_name"
        ).fetchall()
    return [
        {"role": r, "group": g, "created_at": at.isoformat(), "created_by": by} for r, g, at, by in rows
    ]
