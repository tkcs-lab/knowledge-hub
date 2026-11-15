"""The index job worker: claims one queued job at a time and runs it."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from knowledge_hub.config import Config
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


class Worker:
    def __init__(
        self,
        indexer_pool: Any,
        forgejo: Any,
        embedder: Any,
        config: Config,
        blobs: Any = None,
        *,
        chunker_version: int = 1,
        poll_seconds: float = 2.0,
    ) -> None:
        self.pool = indexer_pool
        self.forgejo = forgejo
        self.embedder = embedder
        self.config = config
        self.blobs = blobs
        self.chunker_version = chunker_version
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()

    async def _claim(self) -> tuple[Any, ...] | None:
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(_CLAIM, (MAX_ATTEMPTS,))
            return await cur.fetchone()

    async def _embed_raw(self, payload: Any) -> None:
        """Chunk and embed the blob of one raw record; chunk visibility is inherited by the trigger."""
        if self.blobs is None:
            raise RuntimeError("no blob store configured")
        raw_id = payload["id"]
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT blob_key, required_audiences, allowed_emails FROM kh.raw_records WHERE id = %s",
                (raw_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return  # the record is gone; nothing to index
        blob_key, audiences, emails = row
        text = (await self.blobs.get(blob_key)).decode("utf-8", errors="replace")
        chunks = chunk_markdown(text, str(self.chunker_version))
        visibility = SimpleNamespace(required_audiences=audiences, allowed_emails=emails)
        embeddable = bool(self.embedder.may_embed(visibility))
        vectors: list[list[float] | None] = [None] * len(chunks)
        if embeddable and chunks:
            texts = [f"{c.heading_path}\n\n{c.text}" if c.heading_path else c.text for c in chunks]
            vectors = list(await self.embedder.embed(texts))
        model = self.embedder.model if embeddable else None
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM kh.chunks WHERE raw_id = %s", (raw_id,))
            for c, vec in zip(chunks, vectors, strict=True):
                literal = None if vec is None else "[" + ",".join(repr(float(x)) for x in vec) + "]"
                await conn.execute(
                    "INSERT INTO kh.chunks (id, raw_id, ordinal, heading_path, text, content_sha,"
                    " embedding, embedding_model, owner_key, tier, required_audiences)"
                    " VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s::vector, %s, '', 'T3', '{}')",
                    (raw_id, c.ordinal, c.heading_path, c.text, c.content_sha, literal, model),
                )

    async def _run(self, kind: str, repo: str | None, payload: Any = None) -> None:
        if kind == "git_push":
            if repo is None:
                raise ValueError("git_push job without a repo")
            await sync_repo(
                self.pool,
                self.forgejo,
                self.config,
                repo,
                self.embedder,
                chunker_version=self.chunker_version,
            )
        elif kind == "embed_raw":
            await self._embed_raw(payload)
        # pr_event and embed_derived are recorded as done; later phases handle them.

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

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
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
