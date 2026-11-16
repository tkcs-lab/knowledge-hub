"""The index job worker: claims one queued job at a time and runs it."""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from typing import Any

from knowledge_hub.config import Config
from knowledge_hub.indexer.chunker import chunk_markdown
from knowledge_hub.indexer.sync import sync_repo
from knowledge_hub.proposals import PROPOSE_REF_PREFIX

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 30
PROMOTE_PREFIX = "bot/promote/"

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

    async def _embed_owner(self, column: str, table: str, tier: str, text_column: str, payload: Any) -> None:
        """Chunk and embed one raw or derived record; chunk visibility is inherited by the trigger.

        Existing chunks are deleted before the new ones are inserted, in one transaction, so a retry is safe.
        """
        owner_id = payload["id"]
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {text_column}, required_audiences, allowed_emails FROM kh.{table} WHERE id = %s",
                (owner_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return  # the record is gone; nothing to index
        source, audiences, emails = row
        if column == "raw_id":
            if self.blobs is None:
                raise RuntimeError("no blob store configured")
            source = await self.blobs.get(source)
            source = source.decode("utf-8", errors="replace")
        chunks = chunk_markdown(source, str(self.chunker_version))
        visibility = SimpleNamespace(required_audiences=audiences, allowed_emails=emails)
        embeddable = bool(self.embedder.may_embed(visibility))
        vectors: list[list[float] | None] = [None] * len(chunks)
        if embeddable and chunks:
            texts = [f"{c.heading_path}\n\n{c.text}" if c.heading_path else c.text for c in chunks]
            vectors = list(await self.embedder.embed(texts))
        model = self.embedder.model if embeddable else None
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(f"DELETE FROM kh.chunks WHERE {column} = %s", (owner_id,))
            for c, vec in zip(chunks, vectors, strict=True):
                literal = None if vec is None else "[" + ",".join(repr(float(x)) for x in vec) + "]"
                await conn.execute(
                    f"INSERT INTO kh.chunks (id, {column}, ordinal, heading_path, text, content_sha,"
                    " embedding, embedding_model, owner_key, tier, required_audiences)"
                    " VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s::vector, %s, '', %s, '{}')",
                    (owner_id, c.ordinal, c.heading_path, c.text, c.content_sha, literal, model, tier),
                )

    async def _embed_raw(self, payload: Any) -> None:
        await self._embed_owner("raw_id", "raw_records", "T3", "blob_key", payload)

    async def _embed_derived(self, payload: Any) -> None:
        await self._embed_owner("derived_id", "derived_records", "T2", "body", payload)

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
        elif kind == "embed_derived":
            await self._embed_derived(payload)
        elif kind == "pr_event":
            await self._pr_event(repo, payload)

    async def _alert(self, conn: Any, repo: str, message: str) -> None:
        await conn.execute(
            """
            INSERT INTO kh.index_alerts (repo, path, message)
            SELECT %(repo)s, NULL, %(msg)s WHERE NOT EXISTS (
                SELECT 1 FROM kh.index_alerts
                WHERE repo = %(repo)s AND path IS NULL AND message = %(msg)s)
            """,
            {"repo": repo, "msg": message},
        )

    async def _pr_event(self, repo: str | None, payload: Any) -> None:
        """Apply the outcome of a promotion pull request (plan 3.8, PR events).

        The webhook payload is only a trigger: the PR is re-fetched from Forgejo and its head ref names the
        candidate. Re-running the same event changes nothing.
        """
        number = payload.get("number") if isinstance(payload, dict) else None
        if not repo or not isinstance(number, int) or isinstance(number, bool):
            return
        pull = await self.forgejo.pull(repo, number)
        head = (pull.get("head") or {}).get("ref")
        if isinstance(head, str) and head.startswith(PROPOSE_REF_PREFIX):
            await self._proposal_event(repo, number, pull, bool(pull.get("merged")))
            return
        if not isinstance(head, str) or not head.startswith(PROMOTE_PREFIX):
            return
        try:
            cid = uuid.UUID(head[len(PROMOTE_PREFIX) :])
        except ValueError:
            return
        merged = bool(pull.get("merged"))
        if not merged and pull.get("state") != "closed":
            return  # still open (opened, edited, reopened, ...): nothing to decide
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT target_repo, pr_number FROM kh.candidates WHERE id = %s FOR UPDATE", (cid,)
            )
            row = await cur.fetchone()
            if row is None:
                return
            if row[0] != repo or row[1] != number:
                await self._alert(conn, repo, f"pull request {repo}#{number} does not match candidate {cid}")
                return
            if merged:
                # A reopened and then merged PR may follow a rejection; a promoted candidate stays promoted.
                await conn.execute(
                    "UPDATE kh.candidates SET state = 'promoted', decided_at = now()"
                    " WHERE id = %s AND state IN ('pr_open', 'rejected')",
                    (cid,),
                )
                await conn.execute(
                    """
                    UPDATE kh.conflicts SET state = 'closed', closed_at = now(), closed_by_pr = %s
                    WHERE state = 'open'
                      AND EXISTS (SELECT 1 FROM kh.candidates WHERE id = %s AND state = 'promoted')
                      AND id IN (
                        SELECT i.conflict_id FROM kh.conflict_items i
                        JOIN kh.candidate_sources s
                          ON s.candidate_id = %s AND s.source_kind = i.item_kind AND s.source_id = i.item_id)
                    """,
                    (f"{repo}#{number}", cid, cid),
                )
            else:
                await conn.execute(
                    "UPDATE kh.candidates SET state = 'rejected', decided_at = now()"
                    " WHERE id = %s AND state = 'pr_open'",
                    (cid,),
                )

    async def _proposal_event(self, repo: str, number: int, pull: Any, merged: bool) -> None:
        """Apply the outcome of an agent proposal pull request (plan 3.8, 3.12). Replays change nothing."""
        head = (pull.get("head") or {}).get("ref")
        try:
            pid = uuid.UUID(head[len(PROPOSE_REF_PREFIX) :])
        except ValueError:
            return
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT repo, pr_number, branch FROM kh.proposals WHERE id = %s FOR UPDATE", (pid,)
            )
            row = await cur.fetchone()
            if row is None:
                return
            if row[1] is None and row[2] == head and row[0] == repo:
                await conn.execute(
                    "UPDATE kh.proposals SET pr_number = %s, pr_url = %s WHERE id = %s",
                    (number, pull.get("html_url"), pid),
                )
            elif row[0] != repo or row[1] != number:
                await self._alert(conn, repo, f"pull request {repo}#{number} does not match proposal {pid}")
                return
            if merged:
                await conn.execute(
                    "UPDATE kh.proposals SET state = 'merged', decided_at = now()"
                    " WHERE id = %s AND state IN ('pending', 'pr_open', 'closed', 'failed')",
                    (pid,),
                )
            elif pull.get("state") == "closed":
                await conn.execute(
                    "UPDATE kh.proposals SET state = 'closed', decided_at = now()"
                    " WHERE id = %s AND state IN ('pending', 'pr_open')",
                    (pid,),
                )

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
