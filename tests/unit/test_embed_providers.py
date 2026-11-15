import json

import httpx
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.embed.factory import make_embedder
from knowledge_hub.embed.http import HttpEmbedder
from knowledge_hub.embed.ollama import EmbeddingError, OllamaEmbedder
from knowledge_hub.settings import Settings
from knowledge_hub.visibility import Visibility

DIM = 4


def _ollama(dim=DIM, status=200, seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if seen is not None:
            seen.append((req.url.path, body))
        return httpx.Response(status, json={"embeddings": [[0.1] * dim for _ in body["input"]]})

    return OllamaEmbedder("http://ollama:11434/", "m", DIM, transport=httpx.MockTransport(handler))


async def test_ollama_batches_of_32():
    seen = []
    out = await _ollama(seen=seen).embed([f"t{i}" for i in range(70)])
    assert len(out) == 70
    assert [len(b["input"]) for _, b in seen] == [32, 32, 6]
    assert seen[0][0] == "/api/embed" and seen[0][1]["model"] == "m"


async def test_ollama_errors():
    with pytest.raises(EmbeddingError):
        await _ollama(dim=DIM + 1).embed(["a"])
    with pytest.raises(EmbeddingError):
        await _ollama(status=500).embed(["a"])


def _http(dim=DIM, status=200, seen=None, allowed=("eng", "all")):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if seen is not None:
            seen.append((req.headers.get("authorization"), body))
        return httpx.Response(status, json={"data": [{"embedding": [0.1] * dim} for _ in body["input"]]})

    return HttpEmbedder(
        "http://emb/v1", "tok", "m", DIM, set(allowed), transport=httpx.MockTransport(handler)
    )


async def test_http_batches_auth_and_dimension():
    seen = []
    out = await _http(seen=seen).embed([f"t{i}" for i in range(33)])
    assert len(out) == 33
    assert [len(b["input"]) for _, b in seen] == [32, 1]
    assert seen[0][0] == "Bearer tok"
    with pytest.raises(EmbeddingError):
        await _http(dim=DIM - 1).embed(["a"])
    with pytest.raises(EmbeddingError):
        await _http(status=401).embed(["a"])


@pytest.mark.parametrize(
    ("aud", "emails", "expected"),
    [
        (["eng"], None, True),
        (["eng", "all"], None, True),
        ([], None, False),
        (["eng", "hr"], None, False),
        (["eng"], ["a@x.io"], False),
        (["eng"], [], False),
    ],
)
def test_http_may_embed_truth_table(aud, emails, expected):
    assert _http().may_embed(Visibility(aud, emails)) is expected


def test_ollama_may_embed_always():
    assert _ollama().may_embed(Visibility([], ["a@x.io"])) is True


def test_factory_selection(tmp_path):
    assert isinstance(make_embedder(Settings(env="test", embed_provider="fake")), FakeEmbedder)
    assert isinstance(
        make_embedder(Settings(env="test", embed_provider="ollama", ollama_url="http://o")), OllamaEmbedder
    )
    tok = tmp_path / "tok"
    tok.write_text("secret-for-test\n")
    e = make_embedder(
        Settings(
            env="test",
            embed_provider="http",
            embed_http_url="http://e",
            embed_http_token_file=tok,
            embed_http_allowed_audiences="eng, ops",
        )
    )
    assert isinstance(e, HttpEmbedder) and e.allowed_audiences == {"eng", "ops"}
    with pytest.raises(ValueError):
        make_embedder(Settings(env="test", embed_provider="ollama"))
    with pytest.raises(ValueError):
        make_embedder(Settings(env="test", embed_provider="http"))
