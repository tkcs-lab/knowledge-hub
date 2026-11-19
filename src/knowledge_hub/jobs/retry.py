"""Retry dead ``index_jobs`` and reclaim stuck ones (admin panel, ``Pipeline`` tab)."""

from __future__ import annotations

from typing import Any

from knowledge_hub.indexer.worker import MAX_ATTEMPTS


async def retry_jobs(pool: Any, ids: list[int] | None = None) -> int:
    """Requeue failed jobs that used up their attempts; ``last_error`` is kept.

    Returns the number of jobs requeued.
    """
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            UPDATE kh.index_jobs
               SET state = 'queued', attempts = 0, run_after = now()
             WHERE state = 'failed' AND attempts >= %s
               AND (%s::bigint[] IS NULL OR id = ANY(%s::bigint[]))
            """,
            (MAX_ATTEMPTS, ids, ids),
        )
        return cur.rowcount


async def reclaim_stuck(pool: Any, older_than_minutes: int = 60) -> int:
    """Put ``running`` jobs created more than ``older_than_minutes`` ago back to ``queued``."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            UPDATE kh.index_jobs
               SET state = 'queued', run_after = now()
             WHERE state = 'running'
               AND created_at < now() - make_interval(mins => %s)
            """,
            (older_than_minutes,),
        )
        return cur.rowcount
