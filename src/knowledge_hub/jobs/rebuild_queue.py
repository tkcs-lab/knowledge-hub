"""Queue a rebuild of one repo as a ``git_push`` job run by the hub's own worker."""

from __future__ import annotations

from typing import Any


async def queue_rebuild(pool: Any, repo: str) -> bool:
    """Forget the indexed head of ``repo`` and queue one ``git_push`` job.

    The head is forgotten in any case. Returns False when a queued job, or a running one
    younger than an hour, already exists for the repo.
    """
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM kh.repo_state WHERE repo = %s", (repo,))
        cur = await conn.execute(
            """
            INSERT INTO kh.index_jobs (kind, repo, payload)
            SELECT 'git_push', %s, '{}'::jsonb
             WHERE NOT EXISTS (SELECT 1 FROM kh.index_jobs j
                                WHERE j.kind = 'git_push' AND j.repo = %s
                                  AND (j.state = 'queued'
                                       OR (j.state = 'running' AND j.created_at > now() - interval '1 hour')))
            """,
            (repo, repo),
        )
        return cur.rowcount == 1
