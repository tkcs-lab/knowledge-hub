"""Panel health checks with the pool queries faked and the hub behind a MockTransport."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from knowledge_hub.config import ConfigError, DirectoryConfig, GroupConfig, KhConfig
from knowledge_hub.config_watch import config_digest
from knowledge_hub.panel import health, stats
from knowledge_hub.panel.app import build_panel_app
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.settings import PanelSettings

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _config(**extra) -> KhConfig:
    return KhConfig(
        people={"alice": "alice@example.test"},
        groups={"all": GroupConfig(members=["alice"], approvers=["alice"])},
        **extra,
    )


def _pipeline(**over):
    data = {
        "jobs": {"queued": 0, "running": 0, "done": 0, "failed_retrying": 0, "failed_dead": 0},
        "oldest_queued_age_s": None,
        "stuck_running": 0,
        "failed": [],
        "alerts": [],
        "records_without_chunks": 0,
        "repos": [],
    }
    data.update(over)
    return data


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *a):
        if self.pool.db_error:
            raise ConnectionError("db down")
        return None

    @asynccontextmanager
    async def transaction(self):
        yield

    async def rollback(self):
        return None


class FakePool:
    db_error = False

    @asynccontextmanager
    async def connection(self):
        yield FakeConn(self)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "kh.yaml"
    cfg_file.write_text("admins: [a@example.test]\n")
    box = SimpleNamespace(
        config=_config(), pipeline=_pipeline(), state={"kb-all": ("sha", None)}, snapshot=None
    )

    def load():
        if isinstance(box.config, Exception):
            raise box.config
        return box.config

    async def fake_pipeline(pool):
        if isinstance(box.pipeline, Exception):
            raise box.pipeline
        return box.pipeline

    async def fake_state(pool):
        return box.state

    async def fake_snapshot(conn):
        return box.snapshot

    monkeypatch.setattr(stats, "pipeline", fake_pipeline)
    monkeypatch.setattr(stats, "repo_state", fake_state)
    monkeypatch.setattr(health.store, "load_snapshot", fake_snapshot)
    box.ctx = PanelContext(
        settings=PanelSettings(env="dev", config_file=cfg_file, dev_admin="a@example.test"),
        signer=None,
        upstream=None,
        load_config=load,
        pool=FakePool(),
        hub_settings=SimpleNamespace(
            db_migrator_password_file=Path("/x"), dsn=lambda role: "dsn"
        ),
        now=lambda: NOW.timestamp(),
    )
    box.http = lambda handler: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return box


def _ok_hub(request):
    return httpx.Response(200, text="ok")


async def _run(env, handler=_ok_hub, pending=lambda dsn: []):
    async with env.http(handler) as http:
        return {c["id"]: c for c in await health.run_checks(env.ctx, http=http, pending=pending)}


async def test_nine_checks_in_order(env):
    got = await health.run_checks(env.ctx, http=env.http(_ok_hub), pending=lambda d: [])
    assert [c["id"] for c in got] == [
        "config", "database", "migrations", "hub", "queue", "alerts", "indexed", "embeddings", "directory",
    ]
    assert [c["status"] for c in got] == ["ok"] * 8 + ["n/a"]
    assert all(set(c) == {"id", "status", "detail"} for c in got)


async def test_config(env):
    assert (await _run(env))["config"] == {"id": "config", "status": "ok", "detail": "kh.yaml is valid"}
    env.config = ConfigError("bad yaml")
    got = (await _run(env))["config"]
    assert got["status"] == "fail" and "bad yaml" in got["detail"]


async def test_database(env):
    assert (await _run(env))["database"]["status"] == "ok"
    env.ctx.pool.db_error = True
    got = (await _run(env))["database"]
    assert got["status"] == "fail" and got["detail"].startswith("ConnectionError")


async def test_migrations(env):
    assert (await _run(env))["migrations"]["status"] == "ok"
    got = (await _run(env, pending=lambda d: ["0007_a.sql", "0008_b.sql"]))["migrations"]
    assert got["status"] == "fail" and got["detail"] == "2 pending: 0007_a.sql, 0008_b.sql"

    def boom(dsn):
        raise OSError("no route")

    assert (await _run(env, pending=boom))["migrations"]["status"] == "warn"
    env.ctx.hub_settings.db_migrator_password_file = None
    assert (await _run(env))["migrations"]["status"] == "n/a"


async def test_hub(env):
    assert (await _run(env))["hub"]["status"] == "ok"

    def same(request):
        return httpx.Response(200, headers={"X-KH-Config-Digest": config_digest(env.config)})

    assert (await _run(env, same))["hub"]["status"] == "ok"

    def older(request):
        return httpx.Response(200, headers={"X-KH-Config-Digest": "0" * 64})

    got = (await _run(env, older))["hub"]
    assert got["status"] == "warn" and got["detail"] == "the hub runs an older kh.yaml than the one on disk"

    def down(request):
        raise httpx.ConnectError("refused")

    assert (await _run(env, down))["hub"]["status"] == "fail"
    assert (await _run(env, lambda r: httpx.Response(503)))["hub"]["status"] == "fail"


async def test_queue(env):
    env.pipeline = _pipeline(jobs={**_pipeline()["jobs"], "failed_dead": 2})
    assert (await _run(env))["queue"]["status"] == "fail"
    env.pipeline = _pipeline(stuck_running=1)
    assert (await _run(env))["queue"]["status"] == "warn"
    env.pipeline = _pipeline(oldest_queued_age_s=601)
    assert (await _run(env))["queue"]["status"] == "warn"
    env.pipeline = _pipeline(oldest_queued_age_s=600)
    assert (await _run(env))["queue"]["status"] == "ok"


async def test_alerts(env):
    recent = {"id": 1, "created_at": datetime(2026, 6, 1, 6, tzinfo=UTC).isoformat()}
    old = {"id": 2, "created_at": datetime(2026, 5, 30, tzinfo=UTC).isoformat()}
    env.pipeline = _pipeline(alerts=[recent, old])
    got = (await _run(env))["alerts"]
    assert got["status"] == "warn" and "1 alerts" in got["detail"]
    env.pipeline = _pipeline(alerts=[old])
    assert (await _run(env))["alerts"]["status"] == "ok"


async def test_indexed(env):
    assert (await _run(env))["indexed"]["status"] == "ok"
    env.state = {}
    got = (await _run(env))["indexed"]
    assert got["status"] == "warn" and "kb-all" in got["detail"]


async def test_embeddings(env):
    env.pipeline = _pipeline(records_without_chunks=3)
    assert (await _run(env))["embeddings"]["status"] == "warn"


async def test_directory(env):
    assert (await _run(env))["directory"]["status"] == "n/a"
    env.config = _config(
        directory=DirectoryConfig(
            provider="keycloak", domains=["example.test"], refresh_seconds=300, max_stale_seconds=3600
        )
    )
    assert (await _run(env))["directory"]["status"] == "fail"  # no snapshot
    at = lambda s: datetime.fromtimestamp(NOW.timestamp() - s, UTC)  # noqa: E731
    env.snapshot = ({}, at(10))
    assert (await _run(env))["directory"]["status"] == "ok"
    env.snapshot = ({}, at(700))
    assert (await _run(env))["directory"]["status"] == "warn"
    env.snapshot = ({}, at(4000))
    assert (await _run(env))["directory"]["status"] == "fail"


async def test_one_failure_does_not_stop_the_others(env):
    env.pipeline = RuntimeError("x" * 500)
    env.ctx.pool.db_error = True
    got = await _run(env)
    assert got["queue"]["status"] == "fail" and len(got["queue"]["detail"]) <= len("RuntimeError: ") + 200
    assert got["alerts"]["status"] == "fail" and got["embeddings"]["status"] == "fail"
    assert got["database"]["status"] == "fail"
    assert got["hub"]["status"] == "ok" and got["indexed"]["status"] == "ok"


def test_route(env, monkeypatch):
    async def fake(ctx, **kw):
        return [{"id": "config", "status": "ok", "detail": ""}]

    monkeypatch.setattr("knowledge_hub.panel.api_health.run_checks", fake)
    with TestClient(build_panel_app(env.ctx)) as client:
        body = client.get("/api/health").json()
    assert body["checks"] == [{"id": "config", "status": "ok", "detail": ""}]
    datetime.fromisoformat(body["generated_at"])
