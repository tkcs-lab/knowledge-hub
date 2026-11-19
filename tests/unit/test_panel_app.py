"""Panel app: middlewares, gate, static files."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from knowledge_hub.config import KhConfig
from knowledge_hub.panel import app as app_mod
from knowledge_hub.panel.app import build_panel_app
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.session import Signer
from knowledge_hub.panel.settings import PanelSettings

pytestmark = pytest.mark.unit

PUBLIC = "https://panel.example.test"
ORIGIN_HEADERS = {"Origin": PUBLIC, "Content-Type": "application/json"}


async def _me(request: Request):
    return JSONResponse({"email": request.state.email})


async def _boom(request: Request):
    raise RuntimeError("secret detail")


async def _post(request: Request):
    return JSONResponse({"ok": True})


def _builder(ctx: PanelContext) -> list[Route]:
    return [
        Route("/api/whoami", _me, methods=["GET"]),
        Route("/api/boom", _boom, methods=["GET"]),
        Route("/api/post", _post, methods=["POST"]),
    ]


def _ctx(tmp_path: Path, dev_admin: str | None = None, public: str | None = PUBLIC) -> PanelContext:
    cfg = tmp_path / "kh.yaml"
    cfg.write_text("")
    kw: dict = {"env": "dev", "config_file": cfg, "public_url": public}
    if dev_admin:
        kw["dev_admin"] = dev_admin
    else:
        kw.update(
            oidc_issuer="https://idp.example.test",
            oidc_client_id="c",
            oidc_client_secret_file=cfg,
            oidc_allowed_domains="example.test",
            session_key_file=cfg,
        )
    return PanelContext(
        settings=PanelSettings(**kw),
        signer=Signer(b"k" * 32),
        upstream=None,
        load_config=lambda: KhConfig(admins=["admin@example.test"]),
        now=lambda: 1000.0,
    )


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(app_mod, "ROUTE_BUILDERS", (*app_mod.ROUTE_BUILDERS, _builder))
    app = build_panel_app(_ctx(tmp_path))
    return TestClient(app, base_url=PUBLIC, follow_redirects=False, raise_server_exceptions=False)


def _signed_in(client: TestClient, tmp_path: Path) -> TestClient:
    token = Signer(b"k" * 32).sign("session", {"email": "admin@example.test"}, 3600, 1000.0)
    client.cookies.set("__Host-kh_panel", token, domain="panel.example.test")
    return client


def test_healthz_open_with_local_host(client):
    r = client.get("/healthz", headers={"Host": "127.0.0.1"})
    assert r.status_code == 200
    assert r.text == "ok"
    assert r.headers["content-type"].startswith("text/plain")


def test_api_requires_session(client):
    r = client.get("/api/whoami")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    assert r.headers["cache-control"] == "no-store"


def test_root_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/login"


def test_root_serves_index_with_session(client, tmp_path):
    _signed_in(client, tmp_path)
    r = client.get("/")
    assert r.status_code == 200
    assert "<title>kh admin</title>" in r.text
    assert client.get("/api/whoami").json() == {"email": "admin@example.test"}


def test_wrong_host_is_400(client):
    r = client.get("/api/whoami", headers={"Host": "evil.example"})
    assert r.status_code == 400


def test_csrf_rules(client, tmp_path):
    _signed_in(client, tmp_path)
    assert client.post("/api/post", headers=ORIGIN_HEADERS, content=b"{}").status_code == 200
    for headers in (
        {"Content-Type": "application/json"},
        {"Origin": "https://evil.example", "Content-Type": "application/json"},
        {**ORIGIN_HEADERS, "Sec-Fetch-Site": "cross-site"},
        {"Origin": PUBLIC, "Content-Type": "text/plain"},
    ):
        r = client.post("/api/post", headers=headers, content=b"{}")
        assert r.status_code == 403, headers
        assert r.json()["error"] == "csrf"


def test_security_headers(client):
    r = client.get("/healthz")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    assert "cache-control" not in r.headers


def test_unknown_exception_is_internal(client, tmp_path):
    _signed_in(client, tmp_path)
    r = client.get("/api/boom")
    assert r.status_code == 500
    assert r.json() == {"error": "internal", "detail": ""}
    assert r.headers["cache-control"] == "no-store"


def test_static_served_and_traversal_refused(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    paths = ("/static/../x", "/static/%2e%2e/x", "/static/..%2fapp.py", "/static/%2e%2e%2fpanel%2fapp.py")
    for path in paths:
        assert client.get(path).status_code == 404, path


def test_dev_admin_opens_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "ROUTE_BUILDERS", (*app_mod.ROUTE_BUILDERS, _builder))
    ctx = _ctx(tmp_path, dev_admin="dev@example.test", public=None)
    client = TestClient(build_panel_app(ctx), follow_redirects=False)
    assert client.get("/api/whoami").json() == {"email": "dev@example.test"}
    assert client.get("/").status_code == 200
    headers = {"Origin": "http://testserver", "Content-Type": "application/json"}
    r = client.post("/api/post", headers=headers, content=b"{}")
    assert r.status_code == 200
