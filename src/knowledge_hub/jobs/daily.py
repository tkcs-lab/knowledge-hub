"""``kh daily``: recompute ``stale`` on documents and purge expired replay-cache rows."""

from __future__ import annotations

from typing import Any


async def run_daily(indexer_pool: Any) -> tuple[int, int]:
    """Set ``stale = (review_by < current_date)`` and delete expired ``claim_jti`` rows.

    Returns ``(documents whose stale flag changed, claim rows purged)``. The chunk rows follow the
    document through the ``owner_propagate_to_chunks`` trigger.
    """
    async with indexer_pool.connection() as conn, conn.transaction():
        flipped = await conn.execute(
            "UPDATE kh.documents SET stale = (review_by < current_date)"
            " WHERE stale IS DISTINCT FROM (review_by < current_date)"
        )
        purged = await conn.execute("DELETE FROM kh.claim_jti WHERE expires_at < now()")
        return flipped.rowcount, purged.rowcount
