"""The search statement of plan 3.8: one query, exact vector order, RRF over two pool-limited lists.

Everything the caller controls travels as a bound parameter. The SQL text is assembled only from
the constant fragments below. Row-level security filters ``kh.chunks`` before the pool limit, so a
caller's pool holds only chunks that caller may see.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

TIERS = ("T0", "T0a", "T3")


@dataclass(frozen=True)
class SearchFilters:
    query: str
    query_vector: list[float] | None
    embedding_model: str
    min_tier: str = "T3"
    labels: list[str] = field(default_factory=list)
    audience: str = ""
    limit: int = 10
    offset: int = 0
    pool: int = 50
    min_similarity: float = 0.0
    rrf_k: int = 60
    tier_weights: dict[str, float] = field(default_factory=lambda: {"T0": 1.0, "T0a": 0.7, "T3": 0.4})
    stale_weight: float = 0.5
    include_outdated: bool = False

    def max_distance(self) -> float:
        """Largest cosine distance a vector candidate may have; 2.0 (no limit) when the floor is off."""
        return 2.0 if self.min_similarity <= 0 else 1.0 - self.min_similarity

    def tiers_at_or_above(self) -> list[str]:
        return list(TIERS[: TIERS.index(self.min_tier) + 1])

    def tiers_below(self) -> list[str]:
        return list(TIERS[TIERS.index(self.min_tier) + 1 :])


def parse_tier_weights(text: str) -> dict[str, float]:
    """``"T0:1.0,T0a:0.7,T3:0.4"`` to a dict with all three tiers.

    T0 and T3 are required, T0a is optional (a missing one gets the midpoint), every weight is
    positive, no other tier is allowed, and ``T0 >= T0a >= T3``.
    """
    message = (
        "KH_TIER_WEIGHTS must give a positive weight for T0 and T3, and optionally T0a, "
        "with T0 >= T0a >= T3"
    )
    out: dict[str, float] = {}
    try:
        for part in text.split(","):
            tier, sep, value = part.strip().partition(":")
            tier = tier.strip()
            if not sep or tier not in TIERS or tier in out:
                raise ValueError(message)
            out[tier] = float(value)
    except ValueError:
        raise ValueError(message) from None
    if "T0" not in out or "T3" not in out:
        raise ValueError(message)
    out.setdefault("T0a", (out["T0"] + out["T3"]) / 2)
    if any(not v > 0 for v in out.values()) or not out["T0"] >= out["T0a"] >= out["T3"]:
        raise ValueError(message)
    return {tier: out[tier] for tier in TIERS}


_FILTER = """
       c.tier = ANY(%({tiers})s::text[])
   AND c.labels @> %(labels)s::text[]
   AND (%(audience)s = '' OR c.audience = %(audience)s)
   AND (%(include_outdated)s OR NOT EXISTS (SELECT 1 FROM hidden h WHERE h.owner_key = c.owner_key))
"""


def _lists(prefix: str, tiers: str) -> str:
    """The vector list and the full-text list for one tier set, then their fusion as ``<prefix>_fused``."""
    flt = _FILTER.format(tiers=tiers)
    return f"""
{prefix}_vec AS (
  SELECT id, row_number() OVER (ORDER BY dist, id) AS rnk
    FROM (SELECT c.id, c.embedding <=> %(qvec)s::vector AS dist
            FROM kh.chunks c
           WHERE c.embedding IS NOT NULL AND %(qvec)s::vector IS NOT NULL
             AND c.embedding_model = %(model)s AND {flt}
           ORDER BY dist, c.id
           LIMIT %(pool)s) v
   WHERE %(max_dist)s::float8 >= 2 OR v.dist <= %(max_dist)s::float8
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
WITH hidden AS (
  SELECT 'doc:' || id::text AS owner_key FROM kh.documents WHERE superseded_by IS NOT NULL
  UNION ALL
  SELECT 'rec:' || id::text FROM kh.records WHERE outdated
),
{main_lists},
{low_lists},
scored AS (
  SELECT c.id, c.owner_key, c.tier, c.stale, c.text, c.document_id, c.record_id,
         (h.owner_key IS NOT NULL) AS outdated,
         m.rrf * (%(tier_weights)s::jsonb ->> c.tier)::float8
               * CASE WHEN c.stale OR h.owner_key IS NOT NULL THEN %(stale_weight)s ELSE 1 END AS score,
         CASE WHEN c.embedding IS NOT NULL AND c.embedding_model = %(model)s
              THEN 1 - (c.embedding <=> %(qvec)s::vector) END AS similarity
    FROM main_fused m JOIN kh.chunks c ON c.id = m.id
    LEFT JOIN hidden h ON h.owner_key = c.owner_key
),
best AS (
  SELECT DISTINCT ON (owner_key) * FROM scored ORDER BY owner_key, score DESC, id
),
page AS (
  SELECT * FROM best ORDER BY score DESC, id LIMIT %(limit)s OFFSET %(offset)s
),
low_owners AS (
  SELECT DISTINCT c.owner_key, c.tier
    FROM low_fused l JOIN kh.chunks c ON c.id = l.id
),
meta AS (
  SELECT (SELECT count(*) FROM best) AS total,
         (SELECT count(*) FROM best WHERE tier = 'T0') AS t0_hits,
         (SELECT count(*) FROM best WHERE tier = 'T0a')
           + (SELECT count(*) FROM low_owners WHERE tier = 'T0a') AS t0a_hits,
         (SELECT count(*) FROM best WHERE tier = 'T3')
           + (SELECT count(*) FROM low_owners WHERE tier = 'T3') AS t3_hits,
         (SELECT count(DISTINCT c.owner_key) FROM kh.chunks c, websearch_to_tsquery('english', %(query)s) q
           WHERE c.tsv @@ q.q AND c.tier = 'T0') AS t0_fts_hits,
         (SELECT count(DISTINCT c.owner_key) FROM kh.chunks c, websearch_to_tsquery('english', %(query)s) q
           WHERE c.tsv @@ q.q AND c.tier = 'T3') AS t3_fts_hits
)
SELECT p.id, p.owner_key, p.tier, p.stale, p.outdated, p.text, p.score, p.similarity,
       d.superseded_by AS doc_superseded_by, r.replaced_by AS rec_replaced_by,
       d.repo AS doc_repo, d.path AS doc_path, d.blob_sha AS doc_blob_sha,
       d.title AS doc_title, d.audience AS doc_audience, d.labels AS doc_labels,
       r.title AS rec_title, r.audience AS rec_audience, r.labels AS rec_labels,
       r.source_url AS rec_source_url, r.occurred_at AS rec_occurred_at,
       meta.total, meta.t0_hits, meta.t0a_hits, meta.t3_hits, meta.t0_fts_hits, meta.t3_fts_hits
  FROM meta
  LEFT JOIN page p ON true
  LEFT JOIN kh.documents d ON d.id = p.document_id
  LEFT JOIN kh.records r ON r.id = p.record_id
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
        "qvec": (
            None
            if filters.query_vector is None
            else "[" + ",".join(repr(float(x)) for x in filters.query_vector) + "]"
        ),
        "model": filters.embedding_model,
        "tiers": filters.tiers_at_or_above(),
        "low_tiers": filters.tiers_below(),
        "labels": list(filters.labels),
        "audience": filters.audience,
        "pool": filters.pool,
        "max_dist": filters.max_distance(),
        "rrf_k": filters.rrf_k,
        "limit": filters.limit,
        "offset": filters.offset,
        "tier_weights": json.dumps(filters.tier_weights),
        "stale_weight": filters.stale_weight,
        "include_outdated": filters.include_outdated,
    }
    return sql, params
