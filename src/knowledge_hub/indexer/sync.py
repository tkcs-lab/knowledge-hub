"""Sync one repo's ``main`` into ``kh.documents`` and ``kh.chunks`` (plan section 3.6).

Reads and embeds outside the write transaction; one transaction then deletes vanished rows, upserts
documents, writes chunks, records alerts and moves ``repo_state``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.rows import dict_row

from knowledge_hub.config import KhConfig
from knowledge_hub.forgejo.reader import ForgejoError
from knowledge_hub.indexer.supersede import DocLink, resolve
from knowledge_hub.indexer.sync_chunks import (
    ChunkDoc,
    apply_chunks,
    load_stored_chunks,
    plan_chunks,
)
from knowledge_hub.indexer.sync_plan import SyncPlan, build_sync_plan, changed_paths
from knowledge_hub.indexer.tech_docs import build_tech_plan, changed_tech_paths


async def _load(pool: Any, repo: str) -> tuple[str | None, list[tuple[str, str, str]], set[str], dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT last_indexed_sha FROM kh.repo_state WHERE repo = %s", (repo,))
            state = await cur.fetchone()
            await cur.execute("SELECT path, blob_sha, doc_key FROM kh.documents WHERE repo = %s", (repo,))
            stored = [(r["path"], r["blob_sha"], r["doc_key"]) for r in await cur.fetchall()]
            await cur.execute("SELECT name FROM kh.labels")
            labels = {r["name"] for r in await cur.fetchall()}
        chunks = await load_stored_chunks(conn, repo)
    return (state["last_indexed_sha"] if state else None), stored, labels, chunks


async def _write(conn: Any, plan: SyncPlan, chunk_plan: Any, rewritten: bool) -> None:
    repo = plan.repo
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (repo,))
    keys = [u.doc_key for u in plan.upserts]
    # A renamed file keeps its row (and chunks): only delete a path whose document is not re-upserted.
    if plan.deletes:
        await conn.execute(
            "DELETE FROM kh.documents WHERE repo = %s AND path = ANY(%s) AND NOT (doc_key = ANY(%s))",
            (repo, plan.deletes, keys),
        )
    if keys:
        # Free every path that may change so renames and path swaps cannot hit UNIQUE (repo, path).
        await conn.execute(
            "UPDATE kh.documents SET path = 'moving:' || id::text WHERE repo = %s AND doc_key = ANY(%s)",
            (repo, keys),
        )
    if plan.relabel is not None:
        await conn.execute(
            "UPDATE kh.documents SET labels = %s, updated_at = now()"
            " WHERE repo = %s AND tier = 'T0a' AND labels IS DISTINCT FROM %s",
            (plan.relabel, repo, plan.relabel),
        )
    for u in plan.upserts:
        await conn.execute(
            """
            INSERT INTO kh.documents (repo, path, doc_key, audience, title, body, labels, review_by,
                stale, commit_sha, blob_sha, supersedes, tier)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (repo, doc_key) DO UPDATE SET
                path = EXCLUDED.path, audience = EXCLUDED.audience, title = EXCLUDED.title,
                body = EXCLUDED.body, labels = EXCLUDED.labels, review_by = EXCLUDED.review_by,
                stale = EXCLUDED.stale, commit_sha = EXCLUDED.commit_sha, blob_sha = EXCLUDED.blob_sha,
                supersedes = EXCLUDED.supersedes, tier = EXCLUDED.tier,
                indexed_at = now(), updated_at = now()
            """,
            (
                u.repo, u.path, u.doc_key, u.audience, u.title, u.body, u.labels, u.review_by,
                u.review_by < date.today(), u.commit_sha, u.blob_sha, u.supersedes, u.tier,
            ),
        )  # fmt: skip
    await apply_chunks(conn, chunk_plan)
    await _apply_supersedes(conn, repo)
    for path, message in dict.fromkeys(plan.alerts):
        await _alert(conn, repo, path, message)
    if rewritten:
        await _alert(conn, repo, "", f"main of {repo} was rewritten; run kh rebuild after review")
    await conn.execute(
        "INSERT INTO kh.repo_state (repo, last_indexed_sha) VALUES (%s, %s)"
        " ON CONFLICT (repo) DO UPDATE SET last_indexed_sha = EXCLUDED.last_indexed_sha, indexed_at = now()",
        (repo, plan.head_sha),
    )


async def _apply_supersedes(conn: Any, repo: str) -> None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id::text AS id, doc_key, path, supersedes, superseded_by::text AS superseded_by"
            " FROM kh.documents WHERE repo = %s",
            (repo,),
        )
        rows = await cur.fetchall()
    links = [
        DocLink(r["id"], r["doc_key"], r["path"], tuple(r["supersedes"] or ()), r["superseded_by"])
        for r in rows
    ]
    resolution = resolve(links)
    for link in links:
        new = resolution.links[link.id]
        if new != link.current:
            await conn.execute(
                "UPDATE kh.documents SET superseded_by = %s::uuid WHERE id = %s::uuid"
                " AND superseded_by IS DISTINCT FROM %s::uuid",
                (new, link.id, new),
            )
    for path, message in resolution.alerts:
        await _alert(conn, repo, path, message)


async def _alert(conn: Any, repo: str, path: str, message: str) -> None:
    await conn.execute(
        "INSERT INTO kh.index_alerts (repo, path, message) SELECT %(r)s, %(p)s, %(m)s WHERE NOT EXISTS ("
        " SELECT 1 FROM kh.index_alerts WHERE repo = %(r)s AND path = %(p)s AND message = %(m)s)",
        {"r": repo, "p": path, "m": message},
    )


async def _sync_tech(
    indexer_pool: Any, reader: Any, config: KhConfig, repo: str, embedder: Any, chunker_version: int
) -> SyncPlan:
    _, stored, labels, stored_chunks = await _load(indexer_pool, repo)
    head = await reader.head_sha(repo, await reader.default_branch(repo))
    tree = await reader.list_tree(repo, head)
    texts: dict[str, str | None] = {}
    for path in changed_tech_paths(tree, stored, config.tech_dirs(repo)):
        try:
            texts[path] = await reader.get_raw(repo, path, head)
        except ForgejoError as exc:
            if "not UTF-8" not in str(exc):
                raise
            texts[path] = None
    plan = build_tech_plan(config, repo, head, tree, stored, texts, labels, date.today())
    docs = [ChunkDoc(u.doc_key, u.audience, u.body, stored_chunks.get(u.doc_key, [])) for u in plan.upserts]
    chunk_plan = await plan_chunks(embedder, repo, docs, chunker_version=chunker_version)
    async with indexer_pool.connection() as conn, conn.transaction():
        await _write(conn, plan, chunk_plan, False)
    return plan


async def sync_repo(
    indexer_pool: Any,
    reader: Any,
    config: KhConfig,
    repo: str,
    embedder: Any,
    *,
    chunker_version: int,
    tech_reader: Any = None,
) -> SyncPlan:
    if config.is_tech_repo(repo):
        if tech_reader is None:
            raise ValueError("KH_FORGEJO_URL is required to read the repositories")
        return await _sync_tech(indexer_pool, tech_reader, config, repo, embedder, chunker_version)
    audience = config.group_for_repo(repo)
    if audience is None:
        raise ValueError(f"repo {repo} is in no group")
    if not config.is_active(audience):
        raise ValueError(f"repo {repo} is archived")
    last_sha, stored, labels, stored_chunks = await _load(indexer_pool, repo)
    head = await reader.head_sha(repo, "main")
    rewritten = bool(last_sha) and not await reader.is_ancestor(repo, last_sha, head)
    tree = await reader.list_tree(repo, head)
    texts = {path: await reader.get_raw(repo, path, head) for path in changed_paths(tree, stored)}
    plan = build_sync_plan(config, repo, head, tree, stored, texts, labels)
    docs = [ChunkDoc(u.doc_key, u.audience, u.body, stored_chunks.get(u.doc_key, [])) for u in plan.upserts]
    chunk_plan = await plan_chunks(embedder, repo, docs, chunker_version=chunker_version)
    async with indexer_pool.connection() as conn, conn.transaction():
        await _write(conn, plan, chunk_plan, rewritten)
    return plan
