"""Replay cache shared by all processes, backed by ``kh.claim_seen`` (plan 3.2)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool


class PgReplayCache:
    """``ReplayCache`` on the ``kh_app`` pool.

    The claim verifier calls ``add_if_new`` synchronously from a worker thread
    (``anyio.to_thread``), so the query is handed to the event loop that owns the pool and the
    thread waits for the answer. Calling it on the loop's own thread would deadlock and is refused.
    """

    def __init__(self, pool: AsyncConnectionPool, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._pool = pool
        self._loop = loop or asyncio.get_running_loop()

    async def _add(self, jti: str, expires_at: datetime) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT kh.claim_seen(%s, %s)", (jti, expires_at))
            row = await cur.fetchone()
        return bool(row and row[0])

    def add_if_new(self, jti: str, expires_at: float) -> bool:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            raise RuntimeError("PgReplayCache.add_if_new must run in a worker thread")
        when = datetime.fromtimestamp(expires_at, UTC)
        return asyncio.run_coroutine_threadsafe(self._add(jti, when), self._loop).result()
