"""Ollama embedding provider: ``POST <url>/api/embed`` (plan 3.10)."""

from __future__ import annotations

import httpx

BATCH_SIZE = 32


class EmbeddingError(RuntimeError):
    """An embedding endpoint answered badly (status, shape or dimension)."""


def check_vectors(vectors: object, expected: int, dim: int) -> list[list[float]]:
    """Validate that ``vectors`` holds ``expected`` vectors of length ``dim``."""
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise EmbeddingError(
            f"expected {expected} embeddings, got {len(vectors) if isinstance(vectors, list) else 'none'}"
        )
    for v in vectors:
        if not isinstance(v, list) or len(v) != dim:
            got = len(v) if isinstance(v, list) else "invalid"
            raise EmbeddingError(f"embedding dimension {got} does not match KH_EMBED_DIM {dim}")
    return vectors


class OllamaEmbedder:
    def __init__(
        self,
        url: str,
        model: str,
        dim: int,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.dim = dim
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)

    def may_embed(self, audience: str) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            resp = await self._client.post(
                f"{self.url}/api/embed", json={"model": self.model, "input": batch}
            )
            if resp.status_code != 200:
                raise EmbeddingError(f"ollama returned status {resp.status_code}")
            out.extend(check_vectors(resp.json().get("embeddings"), len(batch), self.dim))
        return out

    async def aclose(self) -> None:
        await self._client.aclose()
