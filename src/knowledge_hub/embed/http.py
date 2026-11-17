"""HTTP embedding provider for a remote endpoint serving the same model (plan 3.10)."""

from __future__ import annotations

import httpx

from knowledge_hub.embed.ollama import BATCH_SIZE, EmbeddingError, check_vectors


class HttpEmbedder:
    def __init__(
        self,
        url: str,
        token: str,
        model: str,
        dim: int,
        allowed_audiences: set[str] | frozenset[str] | list[str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.url = url
        self.model = model
        self.dim = dim
        self.allowed_audiences = frozenset(allowed_audiences)
        self._client = httpx.AsyncClient(
            transport=transport, timeout=timeout, headers={"Authorization": f"Bearer {token}"}
        )

    def may_embed(self, audience: str) -> bool:
        return audience in self.allowed_audiences

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            resp = await self._client.post(self.url, json={"model": self.model, "input": batch})
            if resp.status_code != 200:
                raise EmbeddingError(f"embedding endpoint returned status {resp.status_code}")
            try:
                vectors = [d["embedding"] for d in resp.json()["data"]]
            except (KeyError, TypeError, ValueError) as exc:
                raise EmbeddingError("embedding endpoint returned an unexpected body") from exc
            out.extend(check_vectors(vectors, len(batch), self.dim))
        return out

    async def aclose(self) -> None:
        await self._client.aclose()
