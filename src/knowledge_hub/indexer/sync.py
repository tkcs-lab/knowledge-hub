"""Write one repo sync plan to the database in a single transaction (plan section 3.6).

Writes documents, versions, alerts, ``repo_state`` and chunks. Chunks are embedded before the transaction
opens (``plan_chunks``) and written inside it (``apply_chunks``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from knowledge_hub.config import Config
from knowledge_hub.indexer.sync_chunks import (
    ChunkDoc,
    ChunkPlan,
    apply_chunks,
    load_stored_chunks,
    plan_chunks,
)
from knowledge_hub.indexer.sync_plan import SyncPlan, build_sync_plan
from knowledge_hub.indexer.trust import Verdict

_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


def _parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fmt_ts(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).strftime(_ISO_Z)


async def _load(conn: Any, repo: str) -> tuple[dict | None, dict[str, dict], dict[str, Verdict]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM kh.repo_state WHERE repo = %s", (repo,))
        state = await cur.fetchone()
        await cur.execute(
            "SELECT id, doc_key, path, content_sha, commit_sha, status, approved_by, approved_at, merge_pr,"
            " stamp"
            " FROM kh.documents WHERE repo = %s",
            (repo,),
        )
        rows = await cur.fetchall()
        await cur.execute(
            "SELECT merge_sha, status, stamp, reason, approved_by, approved_at, pr_number, pr_url"
            " FROM kh.merge_verdicts WHERE repo = %s",
            (repo,),
        )
        verdict_rows = await cur.fetchall()
    existing = {}
    for r in rows:
        r["approved_at"] = _fmt_ts(r["approved_at"])
        existing[r["doc_key"]] = r
    verdicts = {
        v["merge_sha"]: Verdict(
            status=v["status"],
            approved_by=tuple(v["approved_by"]),
            approved_at=_fmt_ts(v["approved_at"]),
            merge_pr=v["pr_number"],
            pr_url=v["pr_url"],
            reason=v["reason"],
            stamp=v["stamp"],
        )
        for v in verdict_rows
    }
    return state, existing, verdicts


async def _write(conn: Any, plan: SyncPlan, ids: dict[str, Any]) -> None:
    repo = plan.repo
    if plan.rejudge and not plan.refused:
        await conn.execute("DELETE FROM kh.merge_verdicts WHERE repo = %s", (repo,))
    gone = [*plan.removals, *plan.deletions]
    if gone:
        await conn.execute("DELETE FROM kh.documents WHERE repo = %s AND doc_key = ANY(%s)", (repo, gone))
    # Free every path that is about to change so renames and path swaps cannot hit UNIQUE (repo, path).
    moving = [u.doc_key for u in plan.upserts if u.changed and u.doc_key in ids]
    if moving:
        await conn.execute(
            "UPDATE kh.documents SET path = 'moving:' || id::text WHERE repo = %s AND doc_key = ANY(%s)",
            (repo, moving),
        )
    for u in plan.upserts:
        if not u.changed and u.doc_key in ids:
            continue
        await conn.execute(
            """
            INSERT INTO kh.documents (id, repo, path, doc_key, tier, domain, title, body, frontmatter,
                status, review_by, owner, entities, supersedes, commit_sha, content_sha, approved_by,
                approved_at, merge_pr, pr_url, stamp, required_audiences, allowed_emails)
            VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s)
            ON CONFLICT (repo, doc_key) DO UPDATE SET
                path = EXCLUDED.path, tier = EXCLUDED.tier, domain = EXCLUDED.domain,
                title = EXCLUDED.title, body = EXCLUDED.body, frontmatter = EXCLUDED.frontmatter,
                status = EXCLUDED.status, review_by = EXCLUDED.review_by, owner = EXCLUDED.owner,
                entities = EXCLUDED.entities, supersedes = EXCLUDED.supersedes,
                commit_sha = EXCLUDED.commit_sha, content_sha = EXCLUDED.content_sha,
                approved_by = EXCLUDED.approved_by, approved_at = EXCLUDED.approved_at,
                merge_pr = EXCLUDED.merge_pr, pr_url = EXCLUDED.pr_url, stamp = EXCLUDED.stamp,
                required_audiences = EXCLUDED.required_audiences,
                allowed_emails = EXCLUDED.allowed_emails,
                indexed_at = now(), updated_at = now()
            """,
            (
                repo, u.path, u.doc_key, u.tier, u.domain, u.title, u.body, Jsonb(u.frontmatter),
                u.status, date.fromisoformat(u.review_by), u.owner, u.entities, u.supersedes,
                u.commit_sha, u.content_sha, u.approved_by, _parse_ts(u.approved_at), u.merge_pr,
                u.pr_url, u.stamp, u.required_audiences, u.allowed_emails,
            ),
        )  # fmt: skip
    # Resolve ``superseded_by`` from doc_key to uuid once every document exists.
    for u in plan.upserts:
        await conn.execute(
            """
            UPDATE kh.documents d SET superseded_by = (
                SELECT s.id FROM kh.documents s WHERE s.repo = d.repo AND s.doc_key = %(by)s)
            WHERE d.repo = %(repo)s AND d.doc_key = %(key)s
              AND d.superseded_by IS DISTINCT FROM (
                SELECT s.id FROM kh.documents s WHERE s.repo = d.repo AND s.doc_key = %(by)s)
            """,
            {"repo": repo, "key": u.doc_key, "by": u.superseded_by},
        )
    for v in plan.versions:
        await conn.execute(
            """
            INSERT INTO kh.document_versions
                (document_id, commit_sha, path, tier, status, approved_by, approved_at, stamp)
            SELECT id, %s, %s, %s, %s, %s, %s, %s FROM kh.documents WHERE repo = %s AND doc_key = %s
            ON CONFLICT (document_id, commit_sha) DO NOTHING
            """,
            (
                v.commit_sha, v.path, v.tier, v.status, v.approved_by,
                _parse_ts(v.approved_at), v.stamp, repo, v.doc_key,
            ),
        )  # fmt: skip
    if plan.rejudge:
        await _rejudge_versions(conn, plan)
    for sha, v in plan.verdicts.items():
        await conn.execute(
            """
            INSERT INTO kh.merge_verdicts
                (repo, merge_sha, pr_number, status, stamp, reason, approved_by, approved_at, pr_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (repo, merge_sha) DO NOTHING
            """,
            (
                repo, sha, v.merge_pr, v.status, v.stamp, v.reason, list(v.approved_by),
                _parse_ts(v.approved_at), v.pr_url,
            ),
        )  # fmt: skip
    for a in dict.fromkeys((a.repo, a.path, a.message) for a in plan.alerts):
        await conn.execute(
            """
            INSERT INTO kh.index_alerts (repo, path, message)
            SELECT %(repo)s, %(path)s, %(msg)s WHERE NOT EXISTS (
                SELECT 1 FROM kh.index_alerts
                WHERE repo = %(repo)s AND path IS NOT DISTINCT FROM %(path)s AND message = %(msg)s)
            """,
            {"repo": a[0], "path": a[1], "msg": a[2]},
        )
    rs = plan.repo_state
    if rs is not None:
        await conn.execute(
            """
            INSERT INTO kh.repo_state (repo, trust_anchor_sha, last_indexed_sha, trusted, untrusted_reason)
            VALUES (%(repo)s, %(trust_anchor_sha)s, %(last_indexed_sha)s, %(trusted)s, %(untrusted_reason)s)
            ON CONFLICT (repo) DO UPDATE SET
                trust_anchor_sha = EXCLUDED.trust_anchor_sha,
                last_indexed_sha = EXCLUDED.last_indexed_sha,
                trusted = EXCLUDED.trusted, untrusted_reason = EXCLUDED.untrusted_reason
            """,
            rs,
        )


_VERSION_UPDATE = """
    UPDATE kh.document_versions v SET stamp = %s, approved_by = %s, approved_at = %s,
        status = CASE WHEN %s THEN 'disputed' ELSE COALESCE(%s, v.status) END
    FROM kh.documents d
    WHERE d.id = v.document_id AND d.repo = %s AND v.commit_sha = %s {extra}
"""


async def _rejudge_versions(conn: Any, plan: SyncPlan) -> None:
    """Versions are insert-once; a re-judged merge must change what get_doc(version=...) serves.

    A version is trusted only when the verdict is trusted, the repo is still trusted and, for the
    file at the current head, the plan itself kept it (stamp set); any dispute leaves no stamp.
    """
    repo = plan.repo
    repo_trusted = plan.repo_state is None or bool(plan.repo_state.get("trusted"))
    for sha, v in plan.verdicts.items():
        ok = v.status == "trusted" and repo_trusted
        await conn.execute(
            _VERSION_UPDATE.format(extra=""),
            (
                v.stamp if ok else None, list(v.approved_by) if ok else [],
                _parse_ts(v.approved_at) if ok else None, not ok, None, repo, sha,
            ),
        )  # fmt: skip
    # Files at the current head: the plan's own per-file outcome wins (duplicate id, supersedes...).
    for u in plan.upserts:
        if u.commit_sha not in plan.verdicts:
            continue
        ok = u.stamp is not None and u.status != "disputed"
        await conn.execute(
            _VERSION_UPDATE.format(extra="AND d.doc_key = %s"),
            (
                u.stamp if ok else None, list(u.approved_by) if ok else [],
                _parse_ts(u.approved_at) if ok else None, not ok, u.status if ok else None,
                repo, u.commit_sha, u.doc_key,
            ),
        )  # fmt: skip



async def _chunk_plan(
    embedder: Any, config: Config, plan: SyncPlan, stored: dict, chunker_version: int
) -> ChunkPlan:
    rows = [ChunkDoc(u, stored.get(u.doc_key, [])) for u in plan.upserts]
    cp = await plan_chunks(embedder, config, rows, chunker_version=chunker_version)
    cp.repo = plan.repo
    return cp


async def sync_repo(
    indexer_pool: Any,
    forgejo: Any,
    config: Config,
    repo: str,
    embedder: Any,
    *,
    chunker_version: int = 1,
    rejudge: bool = False,
) -> SyncPlan:
    """Plan and write one repo sync; returns the plan that was written."""
    async with indexer_pool.connection() as conn:
        state, existing, verdicts = await _load(conn, repo)
        stored = await load_stored_chunks(conn, repo)
    plan = await build_sync_plan(
        forgejo, config, repo, state, existing, stored_verdicts=None if rejudge else verdicts, rejudge=rejudge
    )
    chunks = None if plan.refused else await _chunk_plan(embedder, config, plan, stored, chunker_version)
    async with indexer_pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (repo,))
        # Documents may have changed between the read and the lock; re-plan against the locked view.
        locked_state, locked_existing, locked_verdicts = await _load(conn, repo)
        locked_stored = await load_stored_chunks(conn, repo)
        if (locked_state or {}).get("last_indexed_sha") != (state or {}).get("last_indexed_sha") or {
            k: r["commit_sha"] for k, r in locked_existing.items()
        } != {k: r["commit_sha"] for k, r in existing.items()} or locked_verdicts != verdicts:
            plan = await build_sync_plan(
                forgejo,
                config,
                repo,
                locked_state,
                locked_existing,
                stored_verdicts=None if rejudge else locked_verdicts,
                rejudge=rejudge,
            )
            existing = locked_existing
            chunks = (
                None
                if plan.refused
                else await _chunk_plan(embedder, config, plan, locked_stored, chunker_version)
            )
        elif chunks is not None and _ids(locked_stored) != _ids(stored):
            # Chunks were written by someone else meanwhile: diff again (embedding is deterministic per text).
            chunks = await _chunk_plan(embedder, config, plan, locked_stored, chunker_version)
        await _write(conn, plan, {k: r["id"] for k, r in existing.items()})
        if chunks is not None:
            await apply_chunks(conn, chunks)
    return plan


def _ids(stored: dict) -> dict:
    return {k: sorted(str(c.id) for c in v) for k, v in stored.items()}
