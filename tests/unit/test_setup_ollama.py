import json

import httpx
import pytest

from knowledge_hub.settings import Settings
from knowledge_hub.setup.ollama import OllamaStep


def settings(**kw):
    return Settings(ollama_url="http://ollama:11434", embed_model="bge-m3", embed_dim=4, **kw)


def step(handler, **kw):
    return OllamaStep(settings(**kw), transport=httpx.MockTransport(handler))


def server(models, dim=4, calls=None):
    calls = calls if calls is not None else []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1] * dim]})
        if request.url.path == "/api/pull":
            assert json.loads(request.content) == {"model": "bge-m3", "stream": False}
            models.append("bge-m3:latest")
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404)

    return handler, calls


def lines(changes):
    return [c.render() for c in changes]


def test_requires_url():
    with pytest.raises(ValueError, match="KH_OLLAMA_URL is not set"):
        OllamaStep(Settings(ollama_url=None))


async def test_absent():
    handler, calls = server([])
    assert lines(await step(handler).plan()) == ["+ model bge-m3"]
    assert ("POST", "/api/pull") not in calls


async def test_present():
    handler, _ = server(["bge-m3:latest"])
    assert await step(handler).plan() == []


async def test_wrong_dimension():
    handler, _ = server(["bge-m3:latest"], dim=3)
    assert lines(await step(handler).plan()) == ["! model bge-m3: returns 3 dimensions, expected 4"]


async def test_apply_pulls():
    handler, calls = server([])
    assert lines(await step(handler).apply(False)) == ["+ model bge-m3"]
    assert ("POST", "/api/pull") in calls


async def test_apply_present_does_not_pull():
    handler, calls = server(["bge-m3"])
    assert await step(handler).apply(False) == []
    assert ("POST", "/api/pull") not in calls
