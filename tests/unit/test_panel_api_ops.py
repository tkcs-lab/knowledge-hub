"""``/api/ops`` with a fake runner."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.panel.api_ops import build_ops_routes
from knowledge_hub.panel.app import ROUTE_BUILDERS, build_panel_app
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.runner import ApiBusy
from knowledge_hub.panel.settings import PanelSettings

pytestmark = pytest.mark.unit

HEADERS = {"Origin": "http://testserver", "Content-Type": "application/json"}


class FakeRunner:
    def __init__(self):
        self.busy = False
        self.submitted = []
        self.jobs = {"abc": {"id": "abc", "op": "label.add", "state": "done", "ok": True}}

    async def submit(self, valid, email):
        if self.busy:
            raise ApiBusy("busy")
        self.submitted.append((valid.name, dict(valid.args), valid.dry_run, email))
        return "abc"

    def get(self, job_id):
        return self.jobs.get(job_id)


@pytest.fixture
def env(tmp_path: Path):
    cfg_file = tmp_path / "kh.yaml"
    cfg_file.write_text("")
    cfg = KhConfig(
        people={"alice": "alice@example.test"},
        groups={"all": GroupConfig(members=["alice"], approvers=["alice"])},
    )
    ctx = PanelContext(
        settings=PanelSettings(env="dev", config_file=cfg_file, dev_admin="a@example.test"),
        signer=None,
        upstream=None,
        load_config=lambda: cfg,
    )
    runner = FakeRunner()
    return ctx, runner


def _client(env, monkeypatch):
    ctx, runner = env
    monkeypatch.setattr(
        "knowledge_hub.panel.app.ROUTE_BUILDERS",
        tuple(b for b in ROUTE_BUILDERS if b is not build_ops_routes)
        + (lambda c: build_ops_routes(c, runner),),
    )
    return TestClient(build_panel_app(ctx))


def test_builder_is_registered():
    assert build_ops_routes in ROUTE_BUILDERS


def test_submit_then_get(env, monkeypatch):
    with _client(env, monkeypatch) as client:
        body = {"op": "label.archive", "args": {"name": "old"}, "dry_run": True}
        r = client.post("/api/ops", headers=HEADERS, json=body)
        assert r.status_code == 202
        assert r.json() == {"id": "abc", "state": "running"}
        assert env[1].submitted == [("label.archive", {"name": "old"}, True, "a@example.test")]
        got = client.get("/api/ops/abc")
        assert got.status_code == 200 and got.json()["state"] == "done"


def test_validation_error_is_400_with_reason(env, monkeypatch):
    with _client(env, monkeypatch) as client:
        r = client.post("/api/ops", headers=HEADERS, json={"op": "nope", "args": {}})
        assert r.status_code == 400
        assert r.json() == {"error": "bad_args", "detail": "unknown operation"}
        r = client.post("/api/ops", headers=HEADERS, json={"op": "jobs.reclaim", "args": {}, "dry_run": True})
        assert r.status_code == 400 and r.json()["error"] == "bad_args"
    assert env[1].submitted == []


def test_busy_is_409(env, monkeypatch):
    env[1].busy = True
    with _client(env, monkeypatch) as client:
        r = client.post("/api/ops", headers=HEADERS, json={"op": "jobs.reclaim", "args": {}})
    assert r.status_code == 409 and r.json()["error"] == "busy"


def test_unknown_id_is_404(env, monkeypatch):
    with _client(env, monkeypatch) as client:
        r = client.get("/api/ops/zzz")
    assert r.status_code == 404 and r.json()["error"] == "no_such_op"


@pytest.mark.parametrize(
    "body",
    [
        {"op": "jobs.reclaim", "args": {}, "extra": 1},
        {"args": {}},
        [1],
    ],
)
def test_bad_body_is_400(env, monkeypatch, body):
    with _client(env, monkeypatch) as client:
        r = client.post("/api/ops", headers=HEADERS, json=body)
    assert r.status_code == 400 and r.json()["error"] == "bad_args"


def test_non_json_body_is_400(env, monkeypatch):
    with _client(env, monkeypatch) as client:
        r = client.post("/api/ops", headers=HEADERS, content=b"{not json")
    assert r.status_code == 400
