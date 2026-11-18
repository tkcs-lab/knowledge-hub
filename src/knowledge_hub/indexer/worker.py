"""The index job worker: claims one queued job at a time and runs it (plan section 3.6)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from knowledge_hub.config import KhConfig
from knowledge_hub.indexer.chunker import chunk_markdown
from knowledge_hub.indexer.sync import sync_repo

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 30

_CLAIM = """
UPDATE kh.index_jobs SET state = 'running', attempts = attempts + 1
WHERE id = (
    SELECT id FROM kh.index_jobs
    WHERE state IN ('queued', 'failed') AND run_after <= now() AND attempts < %s
    ORDER BY run_after, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, kind, repo, payload, attempts
"""


_POLL_TECH = """
INSERT INTO kh.index_jobs (kind, repo, payload)
SELECT 'git_push', r, '{}'::jsonb FROM unnest(%s::text[]) AS r
WHERE NOT EXISTS (
    SELECT 1 FROM kh.index_jobs j
    WHERE j.kind = 'git_push' AND j.repo = r
      AND (j.state = 'queued' OR (j.state = 'running' AND j.created_at > now() - interval '1 hour'))
)
"""


class Worker:
    def __init__(
        self,
        indexer_pool: Any,
        reader: Any,
        embedder: Any,
        config: KhConfig,
        blobs: Any = None,
        *,
        chunker_version: int = 1,
        poll_seconds: float = 2.0,
        tech_reader: Any = None,
        tech_poll_seconds: float = 0.0,
    ) -> None:
        self.pool = indexer_pool
        self.reader = reader
        self.embedder = embedder
        self.config = config
        self.blobs = blobs
        self.chunker_version = chunker_version
        self.poll_seconds = poll_seconds
        self.tech_reader = tech_reader
        self.tech_poll_seconds = tech_poll_seconds
        self._last_tech_poll: float | None = None
        self._stop = asyncio.Event()
        # sync_repo reads stored chunks before its write transaction and its advisory lock covers only
        # the write, so one sync per repo at a time. Jobs run one by one in this worker; the lock keeps
        # that true if run_once is ever called concurrently.
        self._repo_locks: dict[str, asyncio.Lock] = {}

    async def _claim(self) -> tuple[Any, ...] | None:
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(_CLAIM, (MAX_ATTEMPTS,))
            return await cur.fetchone()

    async def _embed_record(self, payload: Any) -> None:
        """Chunk and embed one T3 record; the chunk trigger copies audience and labels from the record.

        Existing chunks are deleted before the new ones are inserted, in one transaction, so a retry is safe.
        """
        record_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(record_id, str):
            raise ValueError("embed_record job without an id")
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT audience, blob_key FROM kh.records WHERE id = %s", (record_id,))
            row = await cur.fetchone()
        if row is None:
            return  # the record is gone; nothing to index
        audience, blob_key = row
        if self.blobs is None:
            raise RuntimeError("no blob store configured")
        body = (await self.blobs.get(blob_key)).decode("utf-8", errors="replace")
        nul = body.count("\x00")
        if nul:
            body = body.replace("\x00", "")
            log.warning("record %s: removed %d NUL characters before indexing", record_id, nul)
        chunks = chunk_markdown(body, str(self.chunker_version))
        embeddable = bool(self.embedder.may_embed(audience))
        vectors: list[list[float] | None] = [None] * len(chunks)
        if embeddable and chunks:
            texts = [f"{c.heading_path}\n\n{c.text}" if c.heading_path else c.text for c in chunks]
            vectors = list(await self.embedder.embed(texts))
        model = self.embedder.model if embeddable else None
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM kh.chunks WHERE record_id = %s", (record_id,))
            for c, vec in zip(chunks, vectors, strict=True):
                literal = None if vec is None else "[" + ",".join(repr(float(x)) for x in vec) + "]"
                await conn.execute(
                    "INSERT INTO kh.chunks (record_id, ordinal, heading_path, text, content_sha,"
                    " embedding, embedding_model, owner_key, owner_created_at, tier, audience)"
                    " VALUES (%s, %s, %s, %s, %s, %s::vector, %s, '', now(), 'T3', '')",
                    (record_id, c.ordinal, c.heading_path, c.text, c.content_sha, literal, model),
                )

    async def _git_push(self, repo: str | None) -> None:
        if repo is None:
            raise ValueError("git_push job without a repo")
        if not self.config.is_tech_repo(repo):
            group = self.config.group_for_repo(repo)
            if group is not None and not self.config.is_active(group):
                log.info("git_push skipped, group %s is archived", group)
                return
        if self.reader is None:
            raise RuntimeError("no Forgejo reader configured")
        lock = self._repo_locks.setdefault(repo, asyncio.Lock())
        async with lock:
            await sync_repo(
                self.pool,
                self.reader,
                self.config,
                repo,
                self.embedder,
                chunker_version=self.chunker_version,
                tech_reader=self.tech_reader,
            )

    async def _run(self, kind: str, repo: str | None, payload: Any = None) -> None:
        if kind == "git_push":
            await self._git_push(repo)
        elif kind == "embed_record":
            await self._embed_record(payload)
        else:
            raise ValueError(f"unknown job kind {kind}")

    async def run_once(self) -> bool:
        """Run one queued job; False when there was none."""
        job = await self._claim()
        if job is None:
            return False
        job_id, kind, repo, payload, attempts = job
        try:
            await self._run(kind, repo, payload)
        except Exception as exc:
            log.exception("index job %s failed", job_id)
            await self._finish(job_id, attempts, f"{type(exc).__name__}: {exc}")
        else:
            await self._finish(job_id, attempts, None)
        return True

    async def _finish(self, job_id: int, attempts: int, error: str | None) -> None:
        async with self.pool.connection() as conn:
            if error is None:
                await conn.execute(
                    "UPDATE kh.index_jobs SET state = 'done', last_error = NULL WHERE id = %s", (job_id,)
                )
            else:
                # 'failed' with attempts left is retried once run_after has passed (see _CLAIM);
                # after MAX_ATTEMPTS it stays failed for good.
                await conn.execute(
                    "UPDATE kh.index_jobs SET state = 'failed', last_error = %s,"
                    " run_after = now() + make_interval(secs => %s) WHERE id = %s",
                    (error, BACKOFF_SECONDS * attempts, job_id),
                )

    async def poll_tech_repos(self) -> None:
        """Queue one ``git_push`` job per tech repo that has no live job (plan section 3.7)."""
        names = self.config.tech_repo_names
        if not names:
            return
        async with self.pool.connection() as conn:
            await conn.execute(_POLL_TECH, (names,))

    def _tech_poll_due(self) -> bool:
        if self.tech_poll_seconds <= 0 or not self.config.tech_repos:
            return False
        last = self._last_tech_poll
        return last is None or time.monotonic() - last >= self.tech_poll_seconds

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                if self._tech_poll_due():
                    self._last_tech_poll = time.monotonic()
                    await self.poll_tech_repos()
                worked = await self.run_once()
            except Exception:
                log.exception("worker loop error")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self._stop.wait(), self.poll_seconds)
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()
