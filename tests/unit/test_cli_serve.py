"""``kh serve`` exits with 75 when the watcher reports a change (plan 3.2)."""

from __future__ import annotations

import asyncio

import pytest

from knowledge_hub import cli

pytestmark = pytest.mark.unit


class FakeServer:
    instance = None

    def __init__(self, config):
        self.should_exit = False
        FakeServer.instance = self

    async def serve(self):
        while not self.should_exit:
            await asyncio.sleep(0)


@pytest.fixture
def patched(monkeypatch, tmp_path):
    import uvicorn

    import knowledge_hub.app
    import knowledge_hub.deps

    cfg = tmp_path / "kh.yaml"
    cfg.write_text("people:\n  alice: alice@example.test\n")
    monkeypatch.setenv("KH_ENV", "test")
    monkeypatch.setenv("KH_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: object())

    async def build_deps(settings, config):
        return object()

    async def close_deps(deps):
        return None

    monkeypatch.setattr(knowledge_hub.deps, "build_deps", build_deps)
    monkeypatch.setattr(knowledge_hub.deps, "close_deps", close_deps)
    monkeypatch.setattr(knowledge_hub.app, "build_app", lambda settings, deps: object())


def test_change_exits_75(patched, monkeypatch):
    async def fake_watch(path, loaded, interval, on_change, **kw):
        on_change()

    monkeypatch.setattr(cli, "watch", fake_watch)
    assert cli.main(["serve"]) == 75


def test_watch_off_never_starts_and_returns_0(patched, monkeypatch):
    monkeypatch.setenv("KH_CONFIG_WATCH_SECONDS", "0")
    started: list[int] = []

    async def fake_watch(*a, **kw):
        started.append(1)

    monkeypatch.setattr(cli, "watch", fake_watch)

    orig = FakeServer.serve

    async def serve(self):
        self.should_exit = True
        await orig(self)

    monkeypatch.setattr(FakeServer, "serve", serve)
    assert cli.main(["serve"]) == 0
    assert started == []
