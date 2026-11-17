"""The postgres step: roles, database, extension, migrations and groups (plan 3.7 step 1)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from knowledge_hub.config import KhConfig
from knowledge_hub.db.migrate import migrate, pending
from knowledge_hub.settings import Settings
from knowledge_hub.setup.steps import Change

ROLES = ("migrator", "app", "indexer")
EXTENSION = "vector"


class PostgresStep:
    name = "postgres"

    def __init__(self, config: KhConfig, settings: Settings, admin_dsn: str | None) -> None:
        self._config = config
        self._settings = settings
        self._admin = admin_dsn

    # -- helpers -----------------------------------------------------------------

    def _password(self, role: str) -> str | None:
        file: Path | None = getattr(self._settings, f"db_{role}_password_file")
        return file.read_text().strip() if file is not None else None

    def _db_dsn(self, dsn: str) -> str:
        return make_conninfo(dsn, dbname=self._settings.db_name)

    async def _connect(self, dsn: str) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(dsn, autocommit=True)

    async def _server_state(
        self, conn: psycopg.AsyncConnection
    ) -> tuple[dict[str, tuple[bool, bool]], str | None]:
        cur = await conn.execute(
            "SELECT rolname, rolcanlogin AND NOT rolsuper AND NOT rolbypassrls "
            "FROM pg_roles WHERE rolname = ANY (%s)",
            ([f"kh_{r}" for r in ROLES],),
        )
        roles = {name: (True, bool(ok)) for name, ok in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
            (self._settings.db_name,),
        )
        row = await cur.fetchone()
        return roles, (row[0] if row else None)

    async def _has_extension(self, dsn: str) -> bool:
        async with await self._connect(self._db_dsn(dsn)) as conn:
            cur = await conn.execute("SELECT 1 FROM pg_extension WHERE extname = %s", (EXTENSION,))
            return await cur.fetchone() is not None

    # -- plan / apply ------------------------------------------------------------

    async def plan(self) -> list[Change]:
        out, ready = await self._server_changes()
        if ready:
            out += await self._database_changes(prune=None)
        return out

    async def _server_changes(self) -> tuple[list[Change], bool]:
        """Role, database and extension lines; ``ready`` is true when the database is usable."""
        db = self._settings.db_name
        refuse = self._admin is None
        mark = "!" if refuse else "+"
        out: list[Change] = []
        if refuse:
            try:
                conn = await self._connect(make_conninfo(self._settings.dsn("migrator"), dbname="postgres"))
            except psycopg.OperationalError:
                return [
                    Change("!", "role kh_migrator: cannot connect without an admin DSN"),
                    Change("!", f"database {db}: cannot be checked"),
                    Change("!", f"extension {EXTENSION}: cannot be checked"),
                ], False
            probe = self._settings.dsn("migrator")
        else:
            conn = await self._connect(self._admin)  # type: ignore[arg-type]
            probe = self._admin  # type: ignore[assignment]
        async with conn:
            roles, owner = await self._server_state(conn)
        for role in ROLES:
            name = f"kh_{role}"
            if name not in roles:
                out.append(Change(mark, f"role {name}"))  # type: ignore[arg-type]
            elif not roles[name][1]:
                out.append(Change("!" if refuse else "~", f"role {name}: attributes"))
        if owner is None:
            out.append(Change(mark, f"database {db}"))  # type: ignore[arg-type]
        elif owner != "kh_migrator":
            out.append(Change("!" if refuse else "~", f"database {db}: owner is {owner}"))
        if owner is None:
            out.append(Change(mark, f"extension {EXTENSION}"))  # type: ignore[arg-type]
        elif not await self._has_extension(probe):
            out.append(Change(mark, f"extension {EXTENSION}"))  # type: ignore[arg-type]
        return out, not refuse_blocks(out)

    async def _database_changes(self, prune: bool | None) -> list[Change]:
        """Pending migrations and group sync; ``prune`` None means plan only."""
        dsn = self._settings.dsn("migrator")
        out: list[Change] = []
        try:
            names = await asyncio.to_thread(pending, dsn)
        except psycopg.OperationalError:
            names = [n for n, _ in _shipped()]
        if prune is not None and names:
            await asyncio.to_thread(migrate, dsn)
        out += [Change("+", f"migration {n}") for n in names]
        out += await self._groups(dsn, prune)
        return out

    async def _groups(self, dsn: str, prune: bool | None) -> list[Change]:
        out: list[Change] = []
        try:
            conn = await self._connect(dsn)
        except psycopg.OperationalError:
            conn = None
        existing: dict[str, str] = {}
        if conn is not None:
            async with conn:
                cur = await conn.execute("SELECT to_regclass('kh.groups') IS NOT NULL")
                row = await cur.fetchone()
                if row and row[0]:
                    cur = await conn.execute("SELECT name, repo FROM kh.groups")
                    existing = {n: r for n, r in await cur.fetchall()}
                for name in self._config.groups:
                    repo = self._config.repo_for_group(name)
                    if name not in existing:
                        out.append(Change("+", f"group {name}"))
                        if prune is not None:
                            await conn.execute(
                                "INSERT INTO kh.groups (name, repo) VALUES (%s, %s)", (name, repo)
                            )
                    elif existing[name] != repo:
                        out.append(
                            Change("!", f"group {name}: changed repo; remove the group and add a new one")
                        )
                for name in existing:
                    if name in self._config.groups:
                        continue
                    if prune:
                        out.append(await self._remove_group(conn, name, existing[name]))
                    else:
                        out.append(Change("?", f"unmanaged group {name}"))
        else:
            out += [Change("+", f"group {name}") for name in self._config.groups]
        return out

    async def _remove_group(self, conn: psycopg.AsyncConnection, name: str, repo: str) -> Change:
        cur = await conn.execute("SELECT count(*) FROM kh.records WHERE audience = %s", (name,))
        row = await cur.fetchone()
        records = int(row[0]) if row else 0
        if records:
            return Change("!", f"group {name}: still has {records} records")
        async with conn.transaction():
            await conn.execute("DELETE FROM kh.documents WHERE audience = %s", (name,))
            await conn.execute("DELETE FROM kh.repo_state WHERE repo = %s", (repo,))
            await conn.execute("DELETE FROM kh.groups WHERE name = %s", (name,))
        return Change("-", f"group {name}")

    async def apply(self, prune: bool) -> list[Change]:
        out, ready = await self._server_changes()
        if self._admin is not None:
            await self._apply_server()
        if ready:
            out += await self._database_changes(prune=prune)
        return out

    async def _apply_server(self) -> None:
        db = self._settings.db_name
        async with await self._connect(self._admin) as conn:  # type: ignore[arg-type]
            roles, owner = await self._server_state(conn)
            for role in ROLES:
                name = f"kh_{role}"
                verb = "ALTER" if name in roles else "CREATE"
                stmt = sql.SQL(f"{verb} ROLE {{}} LOGIN NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(name))
                password = self._password(role)
                if password is not None:
                    stmt = stmt + sql.SQL(" PASSWORD {}").format(sql.Literal(password))
                await conn.execute(stmt)
            if owner is None:
                await conn.execute(sql.SQL("CREATE DATABASE {} OWNER kh_migrator").format(sql.Identifier(db)))
            elif owner != "kh_migrator":
                await conn.execute(
                    sql.SQL("ALTER DATABASE {} OWNER TO kh_migrator").format(sql.Identifier(db))
                )
        async with await self._connect(self._db_dsn(self._admin)) as conn:  # type: ignore[arg-type]
            await conn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(EXTENSION)))


def refuse_blocks(changes: list[Change]) -> bool:
    """True when a ``!`` line means the database cannot be used yet."""
    return any(c.prefix == "!" for c in changes)


def _shipped() -> list[tuple[str, str]]:
    from knowledge_hub.db.migrate import _migration_files

    return _migration_files()
