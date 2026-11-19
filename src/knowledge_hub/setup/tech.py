"""The tech step: T0a documents of a repo that left ``tech_repos`` are reported, ``--prune`` deletes them.

Unreviewed documents stay readable after their repo is withdrawn from the config until this step removes
them (plan 3.8). A repo that is still configured and every T0 document are never touched.
"""

from __future__ import annotations

import psycopg

from knowledge_hub.config import KhConfig
from knowledge_hub.setup.steps import Change


class TechStep:
    name = "tech"

    def __init__(self, config: KhConfig, migrator_dsn: str) -> None:
        self._config = config
        self._dsn = migrator_dsn

    async def _withdrawn(self, conn: psycopg.AsyncConnection) -> list[str]:
        cur = await conn.execute("SELECT to_regclass('kh.documents') IS NOT NULL")
        row = await cur.fetchone()
        if not row or not row[0]:
            return []
        cur = await conn.execute("SELECT DISTINCT repo FROM kh.documents WHERE tier = 'T0a' ORDER BY repo")
        keep = set(self._config.tech_repo_names)
        return [repo for (repo,) in await cur.fetchall() if repo not in keep]

    async def plan(self) -> list[Change]:
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            return [Change("?", f"tech documents of {repo}: repo is not in tech_repos")
                    for repo in await self._withdrawn(conn)]

    async def apply(self, prune: bool) -> list[Change]:
        out: list[Change] = []
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            for repo in await self._withdrawn(conn):
                if not prune:
                    out.append(Change("?", f"tech documents of {repo}: repo is not in tech_repos"))
                    continue
                await self._delete(conn, repo)
                out.append(Change("-", f"tech documents of {repo}"))
        return out

    async def _delete(self, conn: psycopg.AsyncConnection, repo: str) -> None:
        async with conn.transaction():
            await conn.execute("DELETE FROM kh.documents WHERE tier = 'T0a' AND repo = %s", (repo,))
            await conn.execute("DELETE FROM kh.repo_state WHERE repo = %s", (repo,))

    async def _count(self, conn: psycopg.AsyncConnection, repo: str) -> int:
        cur = await conn.execute("SELECT to_regclass('kh.documents') IS NOT NULL")
        row = await cur.fetchone()
        if not row or not row[0]:
            return 0
        cur = await conn.execute(
            "SELECT count(*) FROM kh.documents WHERE tier = 'T0a' AND repo = %s", (repo,)
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def tech_usage(self, repo: str) -> int:
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            return await self._count(conn, repo)

    async def remove_repo(self, repo: str, *, write: bool) -> Change:
        """Delete the T0a documents and the sync state of one repo (kh repo remove)."""
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            if await self._count(conn, repo) == 0:
                return Change("i", f"tech documents of {repo}: none")
            if write:
                await self._delete(conn, repo)
            return Change("-", f"tech documents of {repo}")
