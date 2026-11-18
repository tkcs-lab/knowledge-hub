"""The labels step: ``kh.labels`` follows the config (plan 3.7 step 2)."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from knowledge_hub.config import KhConfig
from knowledge_hub.setup.steps import Change

IN_USE = "P0K06"


class LabelsStep:
    name = "labels"

    def __init__(self, config: KhConfig, migrator_dsn: str) -> None:
        self._config = config
        self._dsn = migrator_dsn

    async def _existing(self, conn: psycopg.AsyncConnection) -> dict[str, str]:
        cur = await conn.execute("SELECT to_regclass('kh.labels') IS NOT NULL")
        row = await cur.fetchone()
        if not row or not row[0]:
            return {}
        cur = await conn.execute("SELECT name, description FROM kh.labels")
        return {name: description for name, description in await cur.fetchall()}

    def _diff(self, existing: dict[str, str]) -> list[Change]:
        out: list[Change] = []
        for name, description in self._config.labels.items():
            if name not in existing:
                out.append(Change("+", f"label {name}"))
            elif existing[name] != description:
                out.append(Change("~", f"label {name}"))
        for name in existing:
            if name not in self._config.labels:
                out.append(Change("?", f"unmanaged label {name}"))
        return out

    async def plan(self) -> list[Change]:
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            return self._diff(await self._existing(conn))

    async def apply(self, prune: bool) -> list[Change]:
        out: list[Change] = []
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            existing = await self._existing(conn)
            added = 0
            for change in self._diff(existing):
                name = change.text.rsplit(" ", 1)[1]
                if change.prefix in "+~":
                    await conn.execute(
                        "INSERT INTO kh.labels (name, description) VALUES (%s, %s) "
                        "ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description",
                        (name, self._config.labels[name]),
                    )
                    added += change.prefix == "+"
                    out.append(change)
                elif prune:
                    out.append(await self._remove(conn, name))
                else:
                    out.append(change)
            if added:
                for group in self._config.active_groups:
                    repo = self._config.repo_for_group(group)
                    await conn.execute(
                        "INSERT INTO kh.index_jobs (kind, repo, payload) VALUES ('git_push', %s, %s)",
                        (repo, Jsonb({})),
                    )
                    out.append(Change("+", f"job git_push: {repo}"))
        return out

    async def _remove(self, conn: psycopg.AsyncConnection, name: str) -> Change:
        cur = await conn.execute(
            "SELECT (SELECT count(*) FROM kh.documents WHERE %(n)s = ANY (labels))"
            " + (SELECT count(*) FROM kh.records WHERE %(n)s = ANY (labels))",
            {"n": name},
        )
        row = await cur.fetchone()
        used = int(row[0]) if row else 0
        if used:
            return Change("!", f"label {name}: in use")
        try:
            await conn.execute("DELETE FROM kh.labels WHERE name = %s", (name,))
        except psycopg.Error as exc:
            if exc.sqlstate != IN_USE:
                raise
            return Change("!", f"label {name}: in use")
        return Change("-", f"label {name}")
