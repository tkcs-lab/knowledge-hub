"""``kh daily``: recompute ``stale`` on documents."""

from __future__ import annotations

from typing import Any


async def run_daily(indexer_pool: Any) -> int:
    """Set ``stale = (review_by < current_date)``.

    Returns the number of documents whose stale flag changed. The chunk rows follow the
    document through the ``owner_propagate_to_chunks`` trigger.
    """
    async with indexer_pool.connection() as conn, conn.transaction():
        flipped = await conn.execute(
            "UPDATE kh.documents SET stale = (review_by < current_date)"
            " WHERE stale IS DISTINCT FROM (review_by < current_date)"
        )
        return flipped.rowcount


async def purge_identity(indexer_pool: Any) -> int:
    """Delete expired OAuth state through ``kh.oauth_purge()``; return the number of rows removed."""
    async with indexer_pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT kh.oauth_purge()")
        row = await cur.fetchone()
        return int(row[0])


async def purge_dataset_history(indexer_pool: Any, days: int) -> int:
    """Delete dataset history versions closed more than ``days`` days ago; return how many went."""
    async with indexer_pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT kh.dataset_history_purge(%s::int)", (days,))
        row = await cur.fetchone()
        return int(row[0])
