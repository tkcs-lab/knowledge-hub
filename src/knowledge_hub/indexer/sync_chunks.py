"""Chunk planning, embedding and writing for one repo sync (plan sections 3.5 and 3.6).

``plan_chunks`` runs before the write transaction (embedding is slow); ``apply_chunks`` runs inside it.
A stored chunk is kept (same id, same embedding) when a new chunk has the same ``content_sha`` (which
includes the chunker version), the same embedding model and the same "embedded or not" state.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from psycopg.rows import dict_row

from knowledge_hub.indexer.chunker import chunk_markdown


@dataclass
class StoredChunk:
    id: Any
    ordinal: int
    content_sha: str
    embedding_model: str | None
    has_embedding: bool


@dataclass
class ChunkDoc:
    """One document to chunk and the chunks it already has."""

    doc_key: str
    audience: str
    body: str
    stored: list[StoredChunk] = field(default_factory=list)


@dataclass
class NewChunk:
    ordinal: int
    heading_path: str
    text: str
    content_sha: str
    embedding: list[float] | None
    embedding_model: str | None


@dataclass
class DocChunks:
    doc_key: str
    keep: dict[Any, int]  # stored chunk id -> new ordinal
    drop: list[Any]  # stored chunk ids to delete
    write: list[NewChunk]  # chunks to insert


@dataclass
class ChunkPlan:
    repo: str
    docs: list[DocChunks] = field(default_factory=list)


async def load_stored_chunks(conn: Any, repo: str) -> dict[str, list[StoredChunk]]:
    """Stored chunks of every document of ``repo``, by ``doc_key``."""
    out: dict[str, list[StoredChunk]] = defaultdict(list)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT d.doc_key, c.id, c.ordinal, c.content_sha, c.embedding_model,"
            " c.embedding IS NOT NULL AS has_embedding"
            " FROM kh.chunks c JOIN kh.documents d ON d.id = c.document_id WHERE d.repo = %s"
            " ORDER BY c.ordinal",
            (repo,),
        )
        for r in await cur.fetchall():
            out[r["doc_key"]].append(
                StoredChunk(r["id"], r["ordinal"], r["content_sha"], r["embedding_model"], r["has_embedding"])
            )
    return out


def _embed_text(heading_path: str, text: str) -> str:
    return f"{heading_path}\n\n{text}" if heading_path else text


async def plan_chunks(
    embedder: Any, repo: str, docs: Sequence[ChunkDoc], *, chunker_version: int
) -> ChunkPlan:
    """Chunk each document, embed what changed (only when ``may_embed(audience)``), diff against stored."""
    version = str(chunker_version)
    plan = ChunkPlan(repo=repo)
    pending: list[tuple[NewChunk, str]] = []
    for doc in docs:
        embeddable = bool(embedder.may_embed(doc.audience))
        model = embedder.model if embeddable else None
        by_sha: dict[str, list[StoredChunk]] = defaultdict(list)
        for s in doc.stored:
            by_sha[s.content_sha].append(s)
        keep: dict[Any, int] = {}
        write: list[NewChunk] = []
        for c in chunk_markdown(doc.body, version):
            match = next(
                (
                    s
                    for s in by_sha.get(c.content_sha, [])
                    if s.id not in keep and s.embedding_model == model and s.has_embedding == embeddable
                ),
                None,
            )
            if match is not None:
                keep[match.id] = c.ordinal
                continue
            nc = NewChunk(c.ordinal, c.heading_path, c.text, c.content_sha, None, model)
            write.append(nc)
            if embeddable:
                pending.append((nc, _embed_text(c.heading_path, c.text)))
        plan.docs.append(DocChunks(doc.doc_key, keep, [s.id for s in doc.stored if s.id not in keep], write))
    if pending:
        vectors = await embedder.embed([t for _, t in pending])
        for (nc, _), vec in zip(pending, vectors, strict=True):
            nc.embedding = vec
    return plan


def _vec(v: list[float] | None) -> str | None:
    return None if v is None else "[" + ",".join(repr(float(x)) for x in v) + "]"


async def apply_chunks(conn: Any, plan: ChunkPlan) -> None:
    """Write the chunk plan inside the sync transaction (the documents must already be written).

    The ``chunks_inherit_owner`` trigger fills tier, audience, labels, stale and the owner columns.
    """
    for d in plan.docs:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM kh.documents WHERE repo = %s AND doc_key = %s", (plan.repo, d.doc_key)
            )
            found = await cur.fetchone()
        if found is None:
            continue
        doc_id = found[0]
        if d.drop:
            await conn.execute("DELETE FROM kh.chunks WHERE id = ANY(%s)", (d.drop,))
        for chunk_id, ordinal in d.keep.items():
            await conn.execute(
                "UPDATE kh.chunks SET ordinal = %s WHERE id = %s AND ordinal <> %s",
                (ordinal, chunk_id, ordinal),
            )
        for c in d.write:
            await conn.execute(
                "INSERT INTO kh.chunks (document_id, ordinal, heading_path, text, content_sha,"
                " embedding, embedding_model, owner_key, owner_created_at, tier, audience)"
                " VALUES (%s, %s, %s, %s, %s, %s::vector, %s, '', now(), 'T0', '')",
                (
                    doc_id,
                    c.ordinal,
                    c.heading_path,
                    c.text,
                    c.content_sha,
                    _vec(c.embedding),
                    c.embedding_model,
                ),
            )
