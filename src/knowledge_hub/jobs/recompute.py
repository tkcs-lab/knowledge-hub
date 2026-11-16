"""``kh recompute-visibility`` (plan 3.7): re-derive every stored audience from the configuration.

Runs as ``kh_indexer`` in one transaction. The visibility functions of the request path are not
executable by that role, so the union and intersection rules are plain SQL here. Audiences are
clauses: a configured list is one clause (``finance|mgmt``, any member may read), and a derived row
holds the union of its sources' clauses, so the caller must satisfy every source. Recompute only
issues ``UPDATE``s, so the deferred constraint triggers (``INSERT`` only) do not fire.
"""

from __future__ import annotations

from typing import Any

from knowledge_hub.config import Config
from knowledge_hub.indexer.rebuild import CONFIG_REPO, config_hash

# Live visibility of any citable row, keyed the way the source tables name it.
_LIVE = """
    SELECT 'doc' AS kind, id::text AS id, required_audiences AS req, allowed_emails AS emails
      FROM kh.documents
    UNION ALL
    SELECT 'raw', id::text, required_audiences, allowed_emails FROM kh.raw_records
    UNION ALL
    SELECT 'derived', id::text, required_audiences, allowed_emails FROM kh.derived_records
"""

# (source table, parent column, kind column, id column)
_DERIVED = ("derived_sources", "derived_id", "source_kind", "source_id")
_CANDIDATE = ("candidate_sources", "candidate_id", "source_kind", "source_id")
_CONFLICT = ("conflict_items", "conflict_id", "item_kind", "item_id")


async def replace_audience_map(conn: Any, config: Config) -> None:
    """Make ``kh.raw_audience_map`` equal to ``config.raw_audience_map``.

    The exclusive lock waits for in-flight ingests (their trigger reads the map) and holds new ones
    back until commit, so no raw row is stored under the old map after the recompute has run.
    """
    await conn.execute("LOCK TABLE kh.raw_audience_map IN ACCESS EXCLUSIVE MODE")
    await conn.execute("DELETE FROM kh.raw_audience_map")
    for key, audiences in config.raw_audience_map.items():
        kind, _, ident = key.partition(":")
        await conn.execute(
            "INSERT INTO kh.raw_audience_map (kind, key, audiences) VALUES (%s, %s, %s)",
            (kind, ident, sorted(set(audiences))),
        )


async def _recompute_raw(conn: Any) -> None:
    # Same rules as the raw_records_assign_visibility trigger: one clause per matching map entry;
    # a row with neither a mapping nor participants keeps what it has.
    await conn.execute(
        """
        WITH new AS (
            SELECT r.id,
                   (SELECT array_agg(c ORDER BY c COLLATE "C")
                      FROM (SELECT DISTINCT kh.audience_clause(m.audiences) AS c
                              FROM kh.raw_audience_map m
                             WHERE cardinality(m.audiences) > 0
                               AND ((m.kind = 'series' AND m.key = r.series)
                                 OR (m.kind = 'folder' AND m.key = r.folder))) s) AS mapped,
                   (SELECT array_agg(DISTINCT lower(p) ORDER BY lower(p))
                      FROM unnest(r.participants) AS p) AS people
              FROM kh.raw_records r
        ), target AS (
            SELECT id,
                   CASE WHEN mapped IS NOT NULL THEN mapped ELSE '{}'::text[] END AS req,
                   CASE WHEN mapped IS NOT NULL THEN NULL::text[] ELSE people END AS emails
              FROM new WHERE mapped IS NOT NULL OR people IS NOT NULL
        )
        UPDATE kh.raw_records r SET required_audiences = t.req, allowed_emails = t.emails
          FROM target t
         WHERE r.id = t.id
           AND (r.required_audiences, r.allowed_emails) IS DISTINCT FROM (t.req, t.emails)
        """
    )


async def _recompute_documents(conn: Any, config: Config) -> None:
    for name, repo in config.repos.items():
        await conn.execute(
            "UPDATE kh.documents SET required_audiences = %s, allowed_emails = NULL"
            " WHERE repo = %s AND (required_audiences, allowed_emails) IS DISTINCT FROM (%s, NULL::text[])",
            ([repo.audience], name, [repo.audience]),
        )


async def _refresh_and_apply(conn: Any, parent: str, spec: tuple[str, str, str, str], ids: list[Any]) -> None:
    """Refresh the snapshot columns of ``spec`` for the parents ``ids`` from live rows, then set
    each parent to the union of audiences and the intersection of the non-null email lists over
    its sources (live when the source row exists, else the snapshot)."""
    table, pcol, kcol, icol = spec
    await conn.execute(
        f"""
        UPDATE kh.{table} s SET src_required_audiences = l.req, src_allowed_emails = l.emails
          FROM ({_LIVE}) l
         WHERE l.kind = s.{kcol} AND l.id = s.{icol} AND s.{pcol} = ANY(%s)
           AND (s.src_required_audiences, s.src_allowed_emails) IS DISTINCT FROM (l.req, l.emails)
        """,
        (ids,),
    )
    await conn.execute(
        f"""
        WITH src AS (
            SELECT {pcol} AS pid, src_required_audiences AS req, src_allowed_emails AS emails,
                   row_number() OVER (PARTITION BY {pcol} ORDER BY {kcol}, {icol}) AS rn
              FROM kh.{table} WHERE {pcol} = ANY(%s)
        ), lists AS (
            SELECT pid, rn, emails FROM src WHERE emails IS NOT NULL
        ), common AS (
            SELECT l.pid, e.email
              FROM lists l, LATERAL (SELECT DISTINCT x AS email FROM unnest(l.emails) AS x) e
             GROUP BY l.pid, e.email
            HAVING count(*) = (SELECT count(*) FROM lists n WHERE n.pid = l.pid)
        ), agg AS (
            SELECT p.pid,
                   (SELECT coalesce(array_agg(DISTINCT a ORDER BY a), '{{}}'::text[])
                      FROM src s, unnest(s.req) AS a WHERE s.pid = p.pid) AS req,
                   CASE WHEN NOT EXISTS (SELECT 1 FROM lists l WHERE l.pid = p.pid)
                        THEN NULL::text[]
                        ELSE (SELECT coalesce(array_agg(c.email ORDER BY c.email), '{{}}'::text[])
                                FROM common c WHERE c.pid = p.pid)
                   END AS emails
              FROM (SELECT DISTINCT pid FROM src) p
        )
        UPDATE kh.{parent} t SET required_audiences = a.req, allowed_emails = a.emails
          FROM agg a
         WHERE t.id = a.pid AND (t.required_audiences, t.allowed_emails) IS DISTINCT FROM (a.req, a.emails)
        """,
        (ids,),
    )


async def _recompute_derived(conn: Any) -> None:
    cur = await conn.execute("SELECT id FROM kh.derived_records")
    pending = {row[0] for row in await cur.fetchall()}
    cur = await conn.execute(
        "SELECT derived_id, source_id FROM kh.derived_sources WHERE source_kind = 'derived'"
    )
    deps: dict[Any, set[Any]] = {}
    by_text = {str(d): d for d in pending}
    for did, sid in await cur.fetchall():
        if sid in by_text:
            deps.setdefault(did, set()).add(by_text[sid])
    done: set[Any] = set()
    while pending:
        # A derived row waits for the derived rows it cites; cited rows that are gone use snapshots.
        ready = [d for d in pending if deps.get(d, set()) <= done]
        if not ready:
            raise ValueError("derived_sources contain a citation cycle")
        await _refresh_and_apply(conn, "derived_records", _DERIVED, ready)
        done.update(ready)
        pending.difference_update(ready)


async def recompute_in(conn: Any, config: Config) -> str:
    """Recompute inside the caller's transaction (``conn.transaction()``); returns the config hash."""
    await replace_audience_map(conn, config)
    await _recompute_raw(conn)
    await _recompute_documents(conn, config)
    await _recompute_derived(conn)
    for parent, spec in (("candidates", _CANDIDATE), ("conflicts", _CONFLICT)):
        cur = await conn.execute(f"SELECT id FROM kh.{parent}")
        ids = [row[0] for row in await cur.fetchall()]
        if ids:
            await _refresh_and_apply(conn, parent, spec, ids)
    digest = config_hash(config)
    await conn.execute(
        "INSERT INTO kh.repo_state (repo, config_hash) VALUES (%s, %s)"
        " ON CONFLICT (repo) DO UPDATE SET config_hash = EXCLUDED.config_hash",
        (CONFIG_REPO, digest),
    )
    return digest


async def recompute_visibility(indexer_pool: Any, config: Config) -> str:
    """Recompute every stored audience from ``config`` in one transaction."""
    async with indexer_pool.connection() as conn, conn.transaction():
        return await recompute_in(conn, config)
