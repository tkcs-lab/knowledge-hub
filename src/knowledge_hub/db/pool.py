"""Connection pools for the request path (``kh_app``) and the indexer (``kh_indexer``)."""

from __future__ import annotations

from typing import Literal

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from knowledge_hub.settings import Settings


async def _configure(conn: AsyncConnection) -> None:
    await register_vector_async(conn)
    # The adapter lookup above runs a query; end that implicit transaction before the
    # connection goes back to the pool.
    await conn.commit()


def _make_pool(settings: Settings, role: Literal["app", "indexer"]) -> AsyncConnectionPool:
    """Return a pool that is not open yet: the caller awaits ``pool.open()`` (and ``close()``)."""
    return AsyncConnectionPool(
        settings.dsn(role),
        min_size=1,
        max_size=10,
        open=False,
        configure=_configure,
        name=f"kh_{role}",
    )


def make_app_pool(settings: Settings) -> AsyncConnectionPool:
    return _make_pool(settings, "app")


def make_indexer_pool(settings: Settings) -> AsyncConnectionPool:
    return _make_pool(settings, "indexer")
