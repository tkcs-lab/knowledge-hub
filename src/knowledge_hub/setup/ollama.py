"""The ollama step: the embedding model is pulled and has the configured dimension (plan 3.7 step 4)."""

from __future__ import annotations

import httpx

from knowledge_hub.settings import Settings
from knowledge_hub.setup.steps import Change

_PROBE_TEXT = "kh setup probe"


def _same_model(listed: str, wanted: str) -> bool:
    """Ollama lists ``bge-m3:latest`` for a model that was asked for as ``bge-m3``."""
    if listed == wanted:
        return True
    return ":" not in wanted and listed == f"{wanted}:latest"


class OllamaStep:
    name = "ollama"

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.ollama_url:
            raise ValueError("KH_OLLAMA_URL is not set")
        self._model = settings.embed_model
        self._dim = settings.embed_dim
        self._url = settings.ollama_url.rstrip("/")
        self._transport = transport

    def _client(self, timeout: float | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._url, transport=self._transport, timeout=timeout)

    async def _listed(self, client: httpx.AsyncClient) -> bool:
        response = await client.get("/api/tags")
        response.raise_for_status()
        models = response.json().get("models", [])
        return any(_same_model(str(m.get("name", "")), self._model) for m in models)

    async def _dimension_line(self, client: httpx.AsyncClient) -> Change | None:
        response = await client.post("/api/embed", json={"model": self._model, "input": _PROBE_TEXT})
        response.raise_for_status()
        embeddings = response.json().get("embeddings") or [[]]
        n = len(embeddings[0])
        if n != self._dim:
            return Change("!", f"model {self._model}: returns {n} dimensions, expected {self._dim}")
        return None

    async def plan(self) -> list[Change]:
        async with self._client(60.0) as client:
            if not await self._listed(client):
                return [Change("+", f"model {self._model}")]
            line = await self._dimension_line(client)
            return [line] if line else []

    async def apply(self, prune: bool) -> list[Change]:
        async with self._client(None) as client:
            if await self._listed(client):
                line = await self._dimension_line(client)
                return [line] if line else []
            response = await client.post("/api/pull", json={"model": self._model, "stream": False})
            response.raise_for_status()
            out = [Change("+", f"model {self._model}")]
            line = await self._dimension_line(client)
            return out + [line] if line else out
