"""The per-request transaction that carries the caller's identity into row-level security."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from knowledge_hub.auth.claims import AUDIENCE_RE, Caller


@asynccontextmanager
async def caller_tx(
    pool: AsyncConnectionPool, caller: Caller, *, timeout: float | None = None
) -> AsyncIterator[AsyncConnection]:
    """Open a transaction on ``pool`` with the caller's settings applied (plan 3.3).

    The settings are transaction-local (``set_config(..., true)``), so a pooled connection
    carries nothing over to the next request. The transaction commits on a normal exit and
    rolls back on an exception. ``timeout`` bounds the wait for a pooled connection (``None``: the
    pool's default).
    """
    for audience in caller.audiences:
        if not AUDIENCE_RE.fullmatch(audience):
            raise ValueError("invalid audience name")
    audiences = ",".join(sorted(caller.audiences))
    async with pool.connection(timeout=timeout) as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('kh.audiences', %s, true),"
            " set_config('kh.principal_type', %s, true), set_config('kh.sub', %s, true)",
            (audiences, caller.principal_type, caller.sub),
        )
        await conn.execute("SET LOCAL statement_timeout = '5s'")
        yield conn
