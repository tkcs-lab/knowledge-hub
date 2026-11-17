"""The search statement of plan 3.5: one query, exact vector order, RRF over two pool-limited lists.

Everything the caller controls travels as a bound parameter. The SQL text is assembled only from
the constant fragments below. Row-level security filters ``kh.chunks`` before the pool limit, so a
caller's pool holds only chunks that caller may see.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

TIERS = ("T0", "T1", "T2", "T3")


@dataclass(frozen=True)
class SearchFilters:
    query: str
    query_vector: list[float]
    embedding_model: str
    min_tier: str = "T1"
    domains: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    limit: int = 10
    offset: int = 0
    pool: int = 50
    rrf_k: int = 60
    tier_weights: dict[str, float] = field(
        default_factory=lambda: {"T0": 1.0, "T1": 0.85, "T2": 0.6, "T3": 0.4}
    )
    stale_weight: float = 0.5
    superseded_weight: float = 0.3
    disputed_weight: float = 0.5

    def tiers_at_or_above(self) -> list[str]:
        return list(TIERS[: TIERS.index(self.min_tier) + 1])

    def tiers_below(self) -> list[str]:
        return list(TIERS[TIERS.index(self.min_tier) + 1 :])


def parse_tier_weights(text: str) -> dict[str, float]:
    """``"T0:1.0,T1:0.85"`` to a dict; every tier must be present and positive."""
    out: dict[str, float] = {}
    for part in text.split(","):
        tier, _, value = part.strip().partition(":")
        out[tier.strip()] = float(value)
    if set(out) != set(TIERS) or any(v <= 0 for v in out.values()):
        raise ValueError("KH_TIER_WEIGHTS must give a positive weight for each of T0, T1, T2, T3")
    return out


_FILTER = """
       c.tier = ANY(%({tiers})s::text[])
   AND (cardinality(%(domains)s::text[]) = 0 OR c.domain = ANY(%(domains)s::text[]))
   AND (cardinality(%(entities)s::text[]) = 0 OR c.entities && %(entities)s::text[])
"""


def _lists(prefix: str, tiers: str) -> str:
    """The vector list and the full-text list for one tier set, then their fusion as ``<prefix>_fused``."""
    flt = _FILTER.format(tiers=tiers)
    return f"""
{prefix}_vec AS (
  SELECT id, row_number() OVER (ORDER BY dist, id) AS rnk
    FROM (SELECT c.id, c.embedding <=> %(qvec)s::vector AS dist
            FROM kh.chunks c
           WHERE c.embedding IS NOT NULL AND c.embedding_model = %(model)s AND {flt}
           ORDER BY dist, c.id
           LIMIT %(pool)s) v
),
{prefix}_fts AS (
  SELECT id, row_number() OVER (ORDER BY rank DESC, id) AS rnk
    FROM (SELECT c.id, ts_rank_cd(c.tsv, q.q) AS rank
            FROM kh.chunks c, websearch_to_tsquery('english', %(query)s) q
           WHERE c.tsv @@ q.q AND {flt}
           ORDER BY rank DESC, c.id
           LIMIT %(pool)s) f
),
{prefix}_fused AS (
  SELECT coalesce(v.id, f.id) AS id,
         coalesce(1.0 / (%(rrf_k)s + v.rnk), 0) + coalesce(1.0 / (%(rrf_k)s + f.rnk), 0) AS rrf
    FROM {prefix}_vec v FULL OUTER JOIN {prefix}_fts f ON f.id = v.id
)"""


_BODY = """
WITH {main_lists},
{low_lists},
scored AS (
  SELECT c.id, c.owner_key, c.tier, c.domain, c.status, c.stale, c.entities, c.text,
         c.document_id, c.raw_id, c.derived_id, c.owner_created_at, c.agent_written,
         m.rrf * (%(tier_weights)s::jsonb ->> CASE WHEN c.agent_written THEN 'T2' ELSE c.tier END)::float8
               * CASE WHEN c.stale THEN %(stale_weight)s ELSE 1 END
               * CASE WHEN c.superseded THEN %(superseded_weight)s ELSE 1 END
               * CASE WHEN c.status = 'disputed' THEN %(disputed_weight)s ELSE 1 END AS score
    FROM main_fused m JOIN kh.chunks c ON c.id = m.id
),
best AS (
  SELECT DISTINCT ON (owner_key) * FROM scored ORDER BY owner_key, score DESC, id
),
page AS (
  SELECT * FROM best ORDER BY score DESC, id LIMIT %(limit)s OFFSET %(offset)s
),
low_owners AS (
  SELECT DISTINCT ON (c.owner_key) c.owner_key, c.tier, c.owner_created_at
    FROM low_fused l JOIN kh.chunks c ON c.id = l.id
   ORDER BY c.owner_key, c.id
),
meta AS (
  SELECT (SELECT count(*) FROM best) AS total,
         (SELECT count(*) FROM best WHERE tier IN ('T0', 'T1') AND status <> 'disputed'
                                       AND NOT agent_written) AS normative_hits,
         (SELECT count(*) FROM best WHERE agent_written) AS agent_hits,
         (SELECT count(*) FROM low_owners WHERE tier = 'T2') AS low_t2,
         (SELECT count(*) FROM low_owners WHERE tier = 'T3') AS low_t3,
         (SELECT max(owner_created_at) FROM low_owners) AS low_newest_at
)
SELECT p.id, p.owner_key, p.tier, p.domain, p.status, p.stale, p.entities, p.text, p.score, p.agent_written,
       d.repo AS doc_repo, d.path AS doc_path, d.commit_sha AS doc_commit_sha, d.owner AS doc_owner,
       d.approved_by AS doc_approved_by, d.approved_at AS doc_approved_at,
       d.stamp AS doc_stamp, d.review_by AS doc_review_by,
       r.source_url AS raw_source_url,
       meta.total, meta.normative_hits, meta.agent_hits, meta.low_t2, meta.low_t3, meta.low_newest_at
  FROM meta
  LEFT JOIN page p ON true
  LEFT JOIN kh.documents d ON d.id = p.document_id
  LEFT JOIN kh.raw_records r ON r.id = p.raw_id
 ORDER BY p.score DESC NULLS LAST, p.id
"""


def build_search_query(filters: SearchFilters) -> tuple[str, dict[str, Any]]:
    """Return ``(sql, params)`` for ``cursor.execute``; ``params`` is a name-to-value mapping.

    One row per hit of the requested page, each carrying the aggregate columns. When the page is
    empty a single row with null hit columns still carries them.
    """
    sql = _BODY.format(main_lists=_lists("main", "tiers"), low_lists=_lists("low", "low_tiers"))
    params: dict[str, Any] = {
        "query": filters.query,
        "qvec": "[" + ",".join(repr(float(x)) for x in filters.query_vector) + "]",
        "model": filters.embedding_model,
        "tiers": filters.tiers_at_or_above(),
        "low_tiers": filters.tiers_below(),
        "domains": list(filters.domains),
        "entities": list(filters.entities),
        "pool": filters.pool,
        "rrf_k": filters.rrf_k,
        "limit": filters.limit,
        "offset": filters.offset,
        "tier_weights": json.dumps(filters.tier_weights),
        "stale_weight": filters.stale_weight,
        "superseded_weight": filters.superseded_weight,
        "disputed_weight": filters.disputed_weight,
    }
    return sql, params
