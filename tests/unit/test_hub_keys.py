"""Hub keys: format, parsing and the tool allowlist (plan 3.7, 3.10)."""

from __future__ import annotations

import contextlib
import hashlib
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_hub.auth.claims import Call, Caller
from knowledge_hub.auth.keys import KEY_RE, KEY_TOOLS, new_key, parse_key
from knowledge_hub.config import KhConfig
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import TOOL_NOT_ALLOWED, ToolCall, tool_guard
from knowledge_hub.mcp.server import Deps, build_mcp
from knowledge_hub.settings import Settings


def test_new_key_format_and_hash():
    key_id, full, digest = new_key()
    assert KEY_RE.fullmatch(full)
    assert full.startswith(f"khk_{key_id}.")
    secret = full.split(".", 1)[1]
    assert len(secret) == 43
    assert digest == hashlib.sha256(secret.encode("ascii")).digest()
    assert parse_key(full) == (key_id, secret)


def test_two_keys_differ():
    a, b = new_key(), new_key()
    assert a[0] != b[0]
    assert a[1] != b[1]
    assert a[2] != b[2]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "khk_",
        "khk_abcdefghij.short",
        "khk_ABCDEFGHIJ." + "a" * 43,
        "khk_abcdefghi." + "a" * 43,
        "khk_abcdefghij" + "a" * 43,
        "khk_abcdefghij." + "a" * 44,
        "khk_abcdefghij." + "a" * 42 + "=",
        "khk_abcdef1hij." + "a" * 43,
        "kha_abcdefghij." + "a" * 43,
        "khk_abcdefghij." + "a" * 42 + "é",
        " khk_abcdefghij." + "a" * 43,
        "khk_abcdefghij." + "a" * 43 + "\n",
    ],
)
def test_parse_key_refuses(text):
    assert parse_key(text) is None


async def test_key_tools_match_registered_tools():
    deps = Deps(settings=Settings(env="test"), config=KhConfig(), app_pool=None, indexer_pool=None)
    names = {t.name for t in await build_mcp(deps).list_tools()}
    assert set(KEY_TOOLS) == names
    assert list(KEY_TOOLS) == sorted(KEY_TOOLS)


def _ctx(caller, call):
    scope = {"state": {"kh_caller": caller, "kh_call": call}}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(scope=scope)))


def _caller(tools):
    return Caller("key:abc", "n8n_workflow", None, frozenset({"all"}), "abc", tools)


def _guarded(ran):
    deps = SimpleNamespace(ratelimiter=None, app_pool=None)

    @tool_guard("search", deps)
    async def search(call: ToolCall, query: str = "x"):
        ran.append(query)
        return "done"

    return search


async def test_tool_guard_refuses_outside_allowlist_without_calling(monkeypatch):
    ran: list[str] = []
    fn = _guarded(ran)
    ctx = _ctx(_caller(frozenset({"ingest_record"})), Call("search", {"query": "x"}))
    with pytest.raises(ToolError) as info:
        await fn(ctx, query="x")
    assert str(info.value) == TOOL_NOT_ALLOWED == "this credential may not call this tool"
    assert ran == []


@pytest.fixture
def no_db(monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_tx(pool, caller, **kw):
        yield object()

    async def fake_write(deps, caller, events):
        return None

    monkeypatch.setattr(errors, "caller_tx", fake_tx)
    monkeypatch.setattr(errors, "write_events", fake_write)


@pytest.mark.parametrize("tools", [frozenset({"search"}), frozenset({"search", "usage"}), None])
async def test_tool_guard_allows_inside_allowlist_and_none(no_db, tools):
    ran: list[str] = []
    fn = _guarded(ran)
    ctx = _ctx(_caller(tools), Call("search", {"query": "x"}))
    assert await fn(ctx, query="x") == "done"
    assert ran == ["x"]
