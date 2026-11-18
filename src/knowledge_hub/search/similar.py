"""Similar items for a new record or proposed document (plan 3.6): group-local, failure-isolated."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from knowledge_hub.mcp.errors import ToolCall

log = logging.getLogger("knowledge_hub.tools")

EMBED_TIMEOUT_S = 5

_SIMILAR_SQL = """
SELECT c.owner_key, c.tier, 1 - (c.embedding <=> %(qvec)s::vector) AS similarity,
       coalesce(d.title, r.title) AS title
FROM kh.chunks c
LEFT JOIN kh.documents d ON d.id = c.document_id
LEFT JOIN kh.records r ON r.id = c.record_id
WHERE c.embedding IS NOT NULL AND c.embedding_model = %(model)s AND c.audience = %(audience)s
  AND (d.id IS NULL OR d.superseded_by IS NULL)
  AND (r.id IS NULL OR NOT r.outdated)
  AND NOT coalesce(d.repo = %(xrepo)s AND d.doc_key = %(xkey)s, false)
ORDER BY c.embedding <=> %(qvec)s::vector
LIMIT 50
"""


async def similar_items(
    call: ToolCall, text: str, audience: str, *, exclude_repo: str = "", exclude_key: str = ""
) -> list[dict[str, Any]]:
    """Up to ``similar_limit`` items of ``audience`` close to ``text``; any failure gives ``[]``."""
    deps = call.deps
    limit, min_score = deps.settings.similar_limit, deps.settings.similar_min_score
    embedder = deps.embedder
    if limit == 0 or embedder is None or not embedder.may_embed(audience):
        return []
    try:
        (vector,) = await asyncio.wait_for(embedder.embed([text]), EMBED_TIMEOUT_S)
        params = {
            "qvec": "[" + ",".join(repr(float(x)) for x in vector) + "]",
            "model": embedder.model,
            "audience": audience,
            "xrepo": exclude_repo,
            "xkey": exclude_key,
        }
        async with call.conn.transaction():  # a savepoint: a failed lookup must not abort the caller
            async with call.conn.cursor() as cur:
                await cur.execute(_SIMILAR_SQL, params)
                rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - the lookup is optional; the write already succeeded
        log.warning("similar items failed: %s", type(exc).__name__)
        return []
    best: dict[str, tuple[str, float, str]] = {}
    for owner_key, tier, similarity, title in rows:
        if owner_key not in best:  # rows come ordered by distance, so the first is the best
            best[owner_key] = (tier, float(similarity), title)
    out = [
        {"id": key, "tier": tier, "title": title, "similarity": round(sim, 3)}
        for key, (tier, sim, title) in best.items()
        if sim >= min_score
    ]
    return out[:limit]
