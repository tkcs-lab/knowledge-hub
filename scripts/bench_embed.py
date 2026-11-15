"""Benchmark the configured embedding model: chunk throughput and query latency."""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time


def _paragraph(i: int) -> str:
    base = f"Paragraph {i} about product roadmaps, release notes and customer feedback. "
    return (base * (800 // len(base) + 1))[:800]


async def _run(count: int, url: str | None) -> None:
    from knowledge_hub.embed.factory import make_embedder
    from knowledge_hub.settings import Settings

    settings = Settings()
    if url:
        settings = settings.model_copy(update={"ollama_url": url})
    embedder = make_embedder(settings)
    try:
        paragraphs = [_paragraph(i) for i in range(count)]
        start = time.perf_counter()
        await embedder.embed(paragraphs)
        elapsed = time.perf_counter() - start
        latencies = []
        for i in range(20):
            t = time.perf_counter()
            await embedder.embed([f"query {i}: what changed in the roadmap?"])
            latencies.append((time.perf_counter() - t) * 1000)
    finally:
        close = getattr(embedder, "aclose", None)
        if close:
            await close()
    latencies.sort()
    p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
    print(f"model: {embedder.model}")
    print(f"chunks/s: {count / elapsed:.1f}")
    print(f"query mean ms: {statistics.mean(latencies):.1f}")
    print(f"query p95 ms: {p95:.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=200, help="synthetic 800-character paragraphs to embed")
    parser.add_argument("--url", default=None, help="Ollama base URL (overrides KH_OLLAMA_URL)")
    args = parser.parse_args()
    asyncio.run(_run(args.count, args.url))


if __name__ == "__main__":
    main()
