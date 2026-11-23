"""Read-only statistics of the panel: a few ``GROUP BY`` queries as ``kh_indexer`` (plan 3.4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection

from knowledge_hub.indexer.worker import MAX_ATTEMPTS

LIST_LIMIT = 50
ERROR_CHARS = 300
STUCK_MINUTES = 60
RECORD_GRACE_MINUTES = 5


@asynccontextmanager
async def _read(pool: Any) -> AsyncIterator[AsyncConnection]:
    """A connection inside a read-only transaction with a 5 s statement timeout."""
    async with pool.connection() as conn:
        await conn.execute("SET TRANSACTION READ ONLY")
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '5s'")
                yield conn
        finally:
            await conn.rollback()


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None or b is None:
        return a or b
    return max(a, b)


def _empty_stats() -> dict[str, Any]:
    return {
        "documents": {"T0": 0, "T0a": 0},
        "stale_documents": 0,
        "records": 0,
        "outdated_records": 0,
        "chunks": {"T0": 0, "T0a": 0, "T3": 0},
        "embedded_chunks": 0,
        "document_chars": 0,
        "last_indexed_at": None,
        "last_ingested_at": None,
    }


async def group_stats(pool: Any) -> dict[str, dict]:
    """``{audience: stats}`` for every audience that has rows; the caller fills in zeros."""
    out: dict[str, dict] = {}

    def entry(audience: str) -> dict[str, Any]:
        return out.setdefault(audience, _empty_stats())

    async with _read(pool) as conn:
        docs = await (
            await conn.execute(
                "SELECT audience, tier, count(*), count(*) FILTER (WHERE stale),"
                " coalesce(sum(length(body)), 0), max(indexed_at)"
                " FROM kh.documents GROUP BY audience, tier"
            )
        ).fetchall()
        recs = await (
            await conn.execute(
                "SELECT audience, count(*), count(*) FILTER (WHERE outdated), max(ingested_at)"
                " FROM kh.records GROUP BY audience"
            )
        ).fetchall()
        chunks = await (
            await conn.execute(
                "SELECT audience, tier, count(*), count(embedding) FROM kh.chunks GROUP BY audience, tier"
            )
        ).fetchall()
    for audience, tier, n, stale, chars, last in docs:
        e = entry(audience)
        e["documents"][tier] = n
        e["stale_documents"] += stale
        e["document_chars"] += int(chars)
        e["last_indexed_at"] = _later(e["last_indexed_at"], last)
    for audience, n, outdated, last in recs:
        e = entry(audience)
        e["records"] = n
        e["outdated_records"] = outdated
        e["last_ingested_at"] = last
    for audience, tier, n, embedded in chunks:
        e = entry(audience)
        e["chunks"][tier] = n
        e["embedded_chunks"] += embedded
    for e in out.values():
        e["last_indexed_at"] = _iso(e["last_indexed_at"])
        e["last_ingested_at"] = _iso(e["last_ingested_at"])
    return out


def empty_group_stats() -> dict[str, Any]:
    return _empty_stats()


async def label_usage(pool: Any) -> dict[str, dict]:
    """``{label: {"documents": n, "records": n}}`` for labels that are on at least one row."""
    out: dict[str, dict] = {}
    async with _read(pool) as conn:
        for table, key in (("documents", "documents"), ("records", "records")):
            rows = await (
                await conn.execute(
                    f"SELECT l, count(*) FROM kh.{table}, unnest(labels) AS l GROUP BY l"  # noqa: S608
                )
            ).fetchall()
            for label, n in rows:
                out.setdefault(label, {"documents": 0, "records": 0})[key] = n
    return out


async def repo_state(pool: Any) -> dict[str, tuple]:
    """``{repo: (last_indexed_sha, indexed_at as ISO text)}``."""
    async with _read(pool) as conn:
        rows = await (
            await conn.execute("SELECT repo, last_indexed_sha, indexed_at FROM kh.repo_state")
        ).fetchall()
    return {repo: (sha, _iso(at)) for repo, sha, at in rows}


async def document_counts_by_repo(pool: Any) -> dict[str, int]:
    async with _read(pool) as conn:
        rows = await (await conn.execute("SELECT repo, count(*) FROM kh.documents GROUP BY repo")).fetchall()
    return {repo: n for repo, n in rows}


async def dataset_catalog(pool: Any) -> list[dict[str, Any]]:
    """Every dataset with its columns and the planner's row estimate (``None`` when never analysed)."""
    async with _read(pool) as conn:
        datasets = await (
            await conn.execute(
                "SELECT d.name, d.title, d.description, d.audiences, d.title_column, d.history,"
                " d.max_outdated_percent, c.reltuples"
                " FROM kh.datasets d"
                " LEFT JOIN pg_class c ON c.relname = d.name"
                "  AND c.relnamespace = 'kh_data'::regnamespace"
                " ORDER BY d.name"
            )
        ).fetchall()
        columns = await (
            await conn.execute(
                "SELECT dataset, name, type, label, choices, ref_dataset, source_name,"
                " filterable, embed, sensitive, indexed FROM kh.columns ORDER BY dataset, ordinal"
            )
        ).fetchall()
    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for ds, name, typ, label, choices, ref, source, filt, embed, sens, idx in columns:
        by_dataset.setdefault(ds, []).append(
            {
                "name": name,
                "type": typ,
                "label": label,
                "choices": list(choices or []),
                "ref_dataset": ref,
                "source_name": source,
                "filterable": filt,
                "embed": embed,
                "sensitive": sens,
                "indexed": idx,
            }
        )
    out = []
    for name, title, description, audiences, title_column, history, percent, tuples in datasets:
        out.append(
            {
                "name": name,
                "title": title,
                "description": description,
                "audiences": list(audiences),
                "title_column": title_column,
                "history": history,
                "max_outdated_percent": percent,
                "columns": by_dataset.get(name, []),
                "rows_estimate": None if tuples is None or tuples < 0 else int(tuples),
            }
        )
    return out


async def pipeline(pool: Any) -> dict[str, Any]:
    """The ``/api/pipeline`` object."""
    async with _read(pool) as conn:
        jobs = await (
            await conn.execute(
                "SELECT count(*) FILTER (WHERE state = 'queued'),"
                " count(*) FILTER (WHERE state = 'running'),"
                " count(*) FILTER (WHERE state = 'done'),"
                " count(*) FILTER (WHERE state = 'failed' AND attempts < %(max)s),"
                " count(*) FILTER (WHERE state = 'failed' AND attempts >= %(max)s),"
                " extract(epoch FROM now() - min(created_at) FILTER (WHERE state = 'queued')),"
                " count(*) FILTER (WHERE state = 'running'"
                "   AND created_at < now() - make_interval(mins => %(stuck)s))"
                " FROM kh.index_jobs",
                {"max": MAX_ATTEMPTS, "stuck": STUCK_MINUTES},
            )
        ).fetchone()
        failed = await (
            await conn.execute(
                "SELECT id, kind, repo, attempts, left(coalesce(last_error, ''), %s), created_at,"
                " run_after, attempts >= %s FROM kh.index_jobs WHERE state = 'failed'"
                " ORDER BY id DESC LIMIT %s",
                (ERROR_CHARS, MAX_ATTEMPTS, LIST_LIMIT),
            )
        ).fetchall()
        alerts = await (
            await conn.execute(
                "SELECT id, repo, path, message, created_at FROM kh.index_alerts"
                " ORDER BY id DESC LIMIT %s",
                (LIST_LIMIT,),
            )
        ).fetchall()
        no_chunks = (
            await (
                await conn.execute(
                    "SELECT count(*) FROM kh.records r"
                    " WHERE r.ingested_at < now() - make_interval(mins => %s)"
                    " AND NOT EXISTS (SELECT 1 FROM kh.chunks c WHERE c.record_id = r.id)",
                    (RECORD_GRACE_MINUTES,),
                )
            ).fetchone()
        )[0]
        repos = await (
            await conn.execute(
                "SELECT repo, last_indexed_sha, indexed_at FROM kh.repo_state ORDER BY repo"
            )
        ).fetchall()
    queued, running, done, retrying, dead, age, stuck = jobs
    return {
        "jobs": {
            "queued": queued,
            "running": running,
            "done": done,
            "failed_retrying": retrying,
            "failed_dead": dead,
        },
        "oldest_queued_age_s": None if age is None else int(age),
        "stuck_running": stuck,
        "failed": [
            {
                "id": i,
                "kind": kind,
                "repo": repo,
                "attempts": attempts,
                "last_error": err,
                "created_at": _iso(created),
                "run_after": _iso(run_after),
                "dead": is_dead,
            }
            for i, kind, repo, attempts, err, created, run_after, is_dead in failed
        ],
        "alerts": [
            {"id": i, "repo": repo, "path": path, "message": msg, "created_at": _iso(created)}
            for i, repo, path, msg, created in alerts
        ],
        "records_without_chunks": no_chunks,
        "repos": [
            {"repo": repo, "last_indexed_sha": sha, "indexed_at": _iso(at)} for repo, sha, at in repos
        ],
    }
