"""``/healthz`` carries the digest of the loaded config (plan 3.2)."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from knowledge_hub.app import make_healthz
from knowledge_hub.config import KhConfig
from knowledge_hub.config_watch import config_digest

pytestmark = pytest.mark.unit


def test_healthz_answers_ok_with_digest():
    config = KhConfig.model_validate({"people": {"a": "a@x.test"}, "admins": ["a@x.test"]})
    app = Starlette(routes=[Route("/healthz", make_healthz(config))])
    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"
    assert resp.headers["X-KH-Config-Digest"] == config_digest(config)
