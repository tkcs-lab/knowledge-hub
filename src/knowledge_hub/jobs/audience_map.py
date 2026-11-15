"""Load ``config.raw_audience_map`` into ``kh.raw_audience_map`` (plan 3.7)."""

from typing import Any

from knowledge_hub.config import Config
from knowledge_hub.indexer.rebuild import config_hash


async def load_audience_map(indexer_pool: Any, config: Config) -> str:
    """Replace every row of ``raw_audience_map`` in one transaction; returns ``config_hash(config)``."""
    rows = []
    for key, audiences in config.raw_audience_map.items():
        kind, _, ident = key.partition(":")
        rows.append((kind, ident, sorted(set(audiences))))
    async with indexer_pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM kh.raw_audience_map")
        for row in rows:
            await conn.execute(
                "INSERT INTO kh.raw_audience_map (kind, key, audiences) VALUES (%s, %s, %s)", row
            )
    return config_hash(config)
