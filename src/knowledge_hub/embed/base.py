"""Embedding provider protocol and the deterministic test embedder (plan 3.10)."""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Protocol


class EmbeddingProvider(Protocol):
    model: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    def may_embed(self, visibility: object) -> bool: ...


class FakeEmbedder:
    """Unit vectors derived from a SHA-256 stream of the text; same text, same vector."""

    model = "fake"

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def may_embed(self, visibility: object) -> bool:
        return True

    def _one(self, text: str) -> list[float]:
        raw = b""
        counter = 0
        while len(raw) < self.dim * 4:
            raw += hashlib.sha256(counter.to_bytes(4, "big") + text.encode()).digest()
            counter += 1
        vals = [(struct.unpack(">I", raw[i * 4 : i * 4 + 4])[0] / 2**31) - 1.0 for i in range(self.dim)]
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        return [v / norm for v in vals]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]
