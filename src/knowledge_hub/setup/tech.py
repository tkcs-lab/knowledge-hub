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
            return [
                Change("?", f"tech documents of {repo}: repo is not in tech_repos")
                for repo in await self._withdrawn(conn)
            ]

    async def apply(self, prune: bool) -> list[Change]:
        out: list[Change] = []
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            for repo in await self._withdrawn(conn):
                if not prune:
                    out.append(Change("?", f"tech documents of {repo}: repo is not in tech_repos"))
                    continue
                async with conn.transaction():
                    await conn.execute("DELETE FROM kh.documents WHERE tier = 'T0a' AND repo = %s", (repo,))
                    await conn.execute("DELETE FROM kh.repo_state WHERE repo = %s", (repo,))
                out.append(Change("-", f"tech documents of {repo}"))
        return out
