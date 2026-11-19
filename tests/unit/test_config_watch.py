"""``ConfigWatcher`` and ``watch``: debounce, invalid configs, digest rules (plan 3.2)."""

from __future__ import annotations

import logging

import pytest

from knowledge_hub.config import KhConfig, load_config
from knowledge_hub.config_watch import EXIT_RESTART, ConfigWatcher, config_digest, watch

pytestmark = pytest.mark.unit

BASE = "people:\n  alice: alice@example.test\n"
CHANGED = BASE + "  bob: bob@example.test\n"


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "kh.yaml"
    p.write_text(BASE)
    return p


def test_exit_code():
    assert EXIT_RESTART == 75


def test_unchanged_never_fires(path):
    w = ConfigWatcher(path, load_config(path))
    assert [w.poll() for _ in range(4)] == [False] * 4


def test_valid_change_fires_on_second_poll(path):
    w = ConfigWatcher(path, load_config(path))
    path.write_text(CHANGED)
    assert w.poll() is False
    assert w.poll() is True


def test_reverted_change_resets(path):
    w = ConfigWatcher(path, load_config(path))
    path.write_text(CHANGED)
    assert w.poll() is False
    path.write_text(BASE)
    assert w.poll() is False
    path.write_text(CHANGED)
    assert w.poll() is False
    assert w.poll() is True


def test_invalid_change_never_fires_and_logs_once_per_error(path, caplog):
    w = ConfigWatcher(path, load_config(path))
    caplog.set_level(logging.WARNING, logger="knowledge_hub.config_watch")
    path.write_text("people: [\n")
    assert [w.poll() for _ in range(3)] == [False] * 3
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1
    assert msgs[0].startswith("config on disk is invalid; keeping the loaded config: ")
    path.write_text("- not a mapping\n")
    assert w.poll() is False
    assert len(caplog.records) == 2


def test_unreadable_file_is_not_a_change(path):
    w = ConfigWatcher(path, load_config(path))
    path.unlink()
    assert w.poll() is False


def test_admins_and_comment_edits_never_fire(path):
    w = ConfigWatcher(path, load_config(path))
    path.write_text(BASE + "admins:\n  - root@example.test\n")
    assert [w.poll() for _ in range(3)] == [False] * 3
    path.write_text("# a comment\n" + BASE)
    assert [w.poll() for _ in range(3)] == [False] * 3


def test_digest_ignores_key_order_and_admins():
    a = KhConfig.model_validate({"people": {"a": "a@x.test", "b": "b@x.test"}})
    b = KhConfig.model_validate({"people": {"b": "b@x.test", "a": "a@x.test"}, "admins": ["c@x.test"]})
    assert config_digest(a) == config_digest(b)
    c = KhConfig.model_validate({"people": {"a": "a@x.test"}})
    assert config_digest(a) != config_digest(c)


async def test_watch_calls_on_change_once_and_returns(path):
    sleeps: list[float] = []
    calls: list[int] = []

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 1:
            path.write_text(CHANGED)

    await watch(path, load_config(path), 5, lambda: calls.append(1), sleep=sleep)
    assert calls == [1]
    assert sleeps == [5, 5]
