"""``kh daily``: delete usage events older than the retention window."""

from __future__ import annotations

from typing import Any


async def purge_usage(indexer_pool: Any, days: int) -> int:
    """Delete ``kh.usage_events`` rows older than ``days`` days and return how many went."""
    async with indexer_pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "DELETE FROM kh.usage_events WHERE at < now() - make_interval(days => %s::int)", (days,)
        )
        return cur.rowcount
