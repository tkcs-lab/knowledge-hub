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
