"""Load ``config.raw_audience_map`` into ``kh.raw_audience_map`` (plan 3.7)."""

from typing import Any

from knowledge_hub.config import Config
from knowledge_hub.indexer.rebuild import CONFIG_REPO, config_hash
from knowledge_hub.jobs.recompute import recompute_in, replace_audience_map


async def load_audience_map(indexer_pool: Any, config: Config) -> str:
    """Replace every row of ``raw_audience_map`` in one transaction; returns ``config_hash(config)``.

    When a stored config hash exists and differs, every stored audience is recomputed in the same
    transaction, so no row is served under an old map. With no stored hash (nothing was ever
    recorded: a new install or a restore) only the map is loaded; ``kh check`` reports the missing
    hash until the operator runs ``kh recompute-visibility``."""
    digest = config_hash(config)
    async with indexer_pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT config_hash FROM kh.repo_state WHERE repo = %s", (CONFIG_REPO,))
        row = await cur.fetchone()
        if row is None or row[0] is None or row[0] == digest:
            await replace_audience_map(conn, config)
        else:
            await recompute_in(conn, config)
    return digest
