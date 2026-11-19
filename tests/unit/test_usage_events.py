"""Usage events: the searcher hash, the event shapes and the guards of ``write_events``."""

from __future__ import annotations

import re

import pytest

from knowledge_hub.config import KhConfig
from knowledge_hub.mcp.server import Deps
from knowledge_hub.metrics import METRICS
from knowledge_hub.settings import Settings
from knowledge_hub.usage import UsageEvent, searcher_hash, write_events
from tests.support.principals import alice_all, dave_none, wf_token


@pytest.fixture(autouse=True)
def fresh_metrics():
    METRICS.reset()
    yield
    METRICS.reset()


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "cursor.key"
    path.write_bytes(b"k" * 32)
    return path


def make_deps(**settings) -> Deps:
    return Deps(
        settings=Settings(env="test", **settings), config=KhConfig(), app_pool=None, indexer_pool=None
    )


def test_searcher_hash_is_32_hex_and_stable():
    h = searcher_hash(b"k" * 32, "alice")
    assert re.fullmatch(r"[0-9a-f]{32}", h)
    assert h == searcher_hash(b"k" * 32, "alice")
    assert "alice" not in h


def test_searcher_hash_differs_by_sub_and_by_key():
    base = searcher_hash(b"k" * 32, "alice")
    assert base != searcher_hash(b"k" * 32, "bob")
    assert base != searcher_hash(b"j" * 32, "alice")


def test_searched_removes_nul_and_truncates():
    ev = UsageEvent.searched("a\x00b", "T3", 1, 2, ["doc:1"])
    assert ev.query == "ab"
    assert (ev.kind, ev.min_tier, ev.t0_fts_hits, ev.t3_fts_hits, ev.owner_keys) == (
        "search",
        "T3",
        1,
        2,
        ("doc:1",),
    )
    assert len(UsageEvent.searched("x" * 600, "T0", 0, 0, []).query) == 500


def test_opened_shape():
    ev = UsageEvent.opened("rec:x")
    assert (ev.kind, ev.query, ev.min_tier, ev.owner_keys) == ("open", None, None, ("rec:x",))


EVENT = UsageEvent.opened("rec:x")


async def test_empty_events_return_without_a_drop(key_file):
    await write_events(make_deps(cursor_key_file=key_file), alice_all, [])
    assert "kh_usage_events_dropped_total 0" in METRICS.render()


async def test_usage_log_off_returns_without_a_drop(key_file):
    await write_events(make_deps(cursor_key_file=key_file, usage_log=False), alice_all, [EVENT])
    assert "kh_usage_events_dropped_total 0" in METRICS.render()


async def test_n8n_caller_returns_without_a_drop(key_file):
    await write_events(make_deps(cursor_key_file=key_file), wf_token, [EVENT])
    assert "kh_usage_events_dropped_total 0" in METRICS.render()


async def test_caller_without_groups_returns_without_a_drop(key_file):
    await write_events(make_deps(cursor_key_file=key_file), dave_none, [EVENT])
    assert "kh_usage_events_dropped_total 0" in METRICS.render()


async def test_no_key_file_returns_without_a_drop():
    await write_events(make_deps(cursor_key_file=None), alice_all, [EVENT])
    assert "kh_usage_events_dropped_total 0" in METRICS.render()


async def test_control_case_reaches_the_pool(key_file):
    await write_events(make_deps(cursor_key_file=key_file), alice_all, [EVENT])
    assert "kh_usage_events_dropped_total 1" in METRICS.render()
