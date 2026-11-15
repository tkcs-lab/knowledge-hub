"""Choose the deployment's embedding provider from settings (plan 3.10)."""

from __future__ import annotations

from knowledge_hub.embed.base import EmbeddingProvider, FakeEmbedder
from knowledge_hub.embed.http import HttpEmbedder
from knowledge_hub.embed.ollama import OllamaEmbedder
from knowledge_hub.settings import Settings


def make_embedder(settings: Settings) -> EmbeddingProvider:
    """One provider serves documents and queries."""
    provider = settings.embed_provider
    if provider == "fake":
        return FakeEmbedder(settings.embed_dim)
    if provider == "ollama":
        if not settings.ollama_url:
            raise ValueError("KH_OLLAMA_URL is required when KH_EMBED_PROVIDER=ollama")
        return OllamaEmbedder(settings.ollama_url, settings.embed_model, settings.embed_dim)
    if provider == "http":
        if not settings.embed_http_url or settings.embed_http_token_file is None:
            raise ValueError(
                "KH_EMBED_HTTP_URL and KH_EMBED_HTTP_TOKEN_FILE are required when KH_EMBED_PROVIDER=http"
            )
        token = settings.embed_http_token_file.read_text().strip()
        audiences = {a.strip() for a in settings.embed_http_allowed_audiences.split(",") if a.strip()}
        return HttpEmbedder(
            settings.embed_http_url, token, settings.embed_model, settings.embed_dim, audiences
        )
    raise ValueError(f"unknown embed provider {provider!r}")
