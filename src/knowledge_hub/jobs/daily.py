"""``kh daily``: recompute ``stale`` on documents."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("knowledge_hub.jobs")


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


async def purge_records(indexer_pool: Any, blobs: Any, days: int) -> tuple[int, int]:
    """Purge records hidden by a removal more than ``days`` days ago; return ``(purged, blob_failures)``.

    A claim moves rows to reason 'purge' in its own transaction; the blob goes next, and the row
    only in a later transaction, so a record restored in between never loses its body.
    """
    failed: set[Any] = set()
    purged = 0
    for _ in range(20):
        async with indexer_pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT rec_id, rec_blob_key FROM kh.records_purge_claim(%s::int, 200, %s::uuid[])",
                (days, list(failed)),
            )
            claimed = await cur.fetchall()
        if not claimed:
            break
        done = []
        for rec_id, key in claimed:
            try:
                if key:
                    await blobs.delete(key)
            except Exception:
                log.warning("record blob delete failed for %s", rec_id)
                failed.add(rec_id)
            else:
                done.append(rec_id)
        if done:
            async with indexer_pool.connection() as conn, conn.transaction():
                cur = await conn.execute("SELECT kh.records_purge_delete(%s::uuid[])", (done,))
                row = await cur.fetchone()
                purged += int(row[0])
    return purged, len(failed)
