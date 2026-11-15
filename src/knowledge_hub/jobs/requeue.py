"""``kh requeue-embeddings`` (plan 3.7): queue embed jobs for records that have no chunks."""

from __future__ import annotations

from typing import Any


async def requeue_embeddings(indexer_pool: Any) -> int:
    """Insert ``embed_raw`` and ``embed_derived`` jobs for every raw and derived record without chunks
    (and without a job already waiting for it); returns the number of jobs inserted."""
    total = 0
    async with indexer_pool.connection() as conn, conn.transaction():
        for kind, table, column in (
            ("embed_raw", "raw_records", "raw_id"),
            ("embed_derived", "derived_records", "derived_id"),
        ):
            cur = await conn.execute(
                f"""
                INSERT INTO kh.index_jobs (kind, payload)
                SELECT %s, jsonb_build_object('id', r.id::text)
                  FROM kh.{table} r
                 WHERE NOT EXISTS (SELECT 1 FROM kh.chunks c WHERE c.{column} = r.id)
                   AND NOT EXISTS (SELECT 1 FROM kh.index_jobs j
                                    WHERE j.kind = %s AND j.state IN ('queued', 'running')
                                      AND j.payload ->> 'id' = r.id::text)
                 ORDER BY r.id
                """,
                (kind, kind),
            )
            total += cur.rowcount
    return total
