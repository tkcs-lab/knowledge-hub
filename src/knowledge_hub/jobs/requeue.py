"""``kh requeue-embeddings``: queue an ``embed_record`` job for every record that has no chunks."""

from __future__ import annotations

from typing import Any


async def requeue_embeddings(indexer_pool: Any) -> int:
    """Insert one job per record without chunks, skipping records with a job already waiting.

    Returns the number of jobs inserted.
    """
    async with indexer_pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO kh.index_jobs (kind, payload)
            SELECT 'embed_record', jsonb_build_object('id', r.id::text)
              FROM kh.records r
             WHERE NOT EXISTS (SELECT 1 FROM kh.chunks c WHERE c.record_id = r.id)
               AND NOT EXISTS (SELECT 1 FROM kh.index_jobs j
                                WHERE j.kind = 'embed_record' AND j.state IN ('queued', 'running')
                                  AND j.payload ->> 'id' = r.id::text)
             ORDER BY r.id
            """
        )
        return cur.rowcount
