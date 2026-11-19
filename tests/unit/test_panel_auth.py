"""Panel sign-in routes over ASGI with a stub upstream."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from knowledge_hub.config import ConfigError, KhConfig
from knowledge_hub.oauth.upstream import UpstreamError, VerifiedIdentity
from knowledge_hub.panel.auth import build_auth_routes, current_admin
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.session import Signer
from knowledge_hub.panel.settings import PanelSettings

pytestmark = pytest.mark.unit

PUBLIC = "https://panel.example.test"
IDP = "https://idp.example.test/auth"


class StubUpstream:
    def __init__(self) -> None:
        self.email = "admin@example.test"
        self.error: UpstreamError | None = None
        self.nonces: list[str] = []

    def new_pkce(self) -> tuple[str, str]:
        return "verifier", "challenge"

    async def authorization_url(self, state: str, nonce: str, challenge: str) -> str:
        return f"{IDP}?state={state}"

    async def exchange(self, code: str, verifier: str) -> dict[str, Any]:
        if self.error:
            raise self.error
        assert verifier == "verifier"
        return {"id_token": "t"}

    async def verify_id_token(self, id_token: str, nonce: str) -> VerifiedIdentity:
        self.nonces.append(nonce)
        return VerifiedIdentity(email=self.email, sub="s", hd=None)


class Env:
    def __init__(self, tmp_path: Path, public: str | None = PUBLIC, dev_admin: str | None = None) -> None:
        self.admins = ["admin@example.test"]
        self.broken = False
        self.upstream = StubUpstream()
        self.clock = 1000.0
        self.state = ""
        cfg = tmp_path / "kh.yaml"
        cfg.write_text("")
        kw: dict[str, Any] = {"env": "dev", "config_file": cfg, "public_url": public}
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
        self.ctx = PanelContext(
            settings=PanelSettings(**kw),
            signer=Signer(b"k" * 32),
            upstream=self.upstream,
            load_config=self._load,
            now=lambda: self.clock,
        )
        app = Starlette(routes=build_auth_routes(self.ctx))
        self.client = TestClient(app, base_url=public or "http://testserver", follow_redirects=False)

    def _load(self) -> KhConfig:
        if self.broken:
            raise ConfigError("bad")
        return KhConfig(admins=list(self.admins))

    def start(self) -> None:
        r = self.client.get("/auth/login")
        assert r.status_code == 302
        self.state = r.headers["location"].split("state=")[1]

    def callback(self, state: str | None = None) -> Any:
        return self.client.get("/auth/callback", params={"code": "c", "state": state or self.state})

    def request(self) -> Request:
        cookie = self.client.cookies.get("__Host-kh_panel")
        headers = [(b"cookie", f"__Host-kh_panel={cookie}".encode())] if cookie else []
        return Request({"type": "http", "headers": headers, "method": "GET", "path": "/"})


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_login_sets_flow_cookie_and_redirects(env: Env) -> None:
    r = env.client.get("/auth/login")
    assert r.status_code == 302
    assert r.headers["location"].startswith(IDP + "?state=")
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-kh_panel_flow=")
    assert "Max-Age=600" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/" in cookie


def test_insecure_public_url_has_plain_cookie_names(tmp_path: Path) -> None:
    e = Env(tmp_path, public="http://localhost:8081")
    r = e.client.get("/auth/login")
    assert r.headers["set-cookie"].startswith("kh_panel_flow=")
    assert "Secure" not in r.headers["set-cookie"]


def test_callback_wrong_state_is_400(env: Env) -> None:
    env.start()
    r = env.callback(state="other")
    assert r.status_code == 400 and r.text == "invalid request"


def test_callback_without_cookie_is_400(env: Env) -> None:
    r = env.client.get("/auth/callback", params={"code": "c", "state": "x"})
    assert r.status_code == 400 and r.text == "invalid request"


def test_admin_gets_session_cookie_and_page(env: Env) -> None:
    env.start()
    r = env.callback()
    assert r.status_code == 200
    assert 'http-equiv="refresh" content="0;url=/"' in r.text
    assert ">Continue</a>" in r.text
    assert "<script" not in r.text and "style" not in r.text
    cookies = [v for k, v in r.headers.multi_items() if k == "set-cookie"]
    session = next(c for c in cookies if c.startswith("__Host-kh_panel="))
    assert "samesite=strict" in session.lower()
    assert "HttpOnly" in session and "Secure" in session
    assert f"Max-Age={8 * 3600}" in session
    flow = next(c for c in cookies if c.startswith("__Host-kh_panel_flow="))
    assert "Max-Age=0" in flow
    assert len(env.upstream.nonces) == 1


def test_non_admin_is_403(env: Env) -> None:
    env.upstream.email = "user@example.test"
    env.start()
    r = env.callback()
    assert r.status_code == 403 and r.text == "not an administrator"
    assert env.client.cookies.get("__Host-kh_panel") is None


def test_email_case_is_folded(env: Env) -> None:
    env.upstream.email = "Admin@Example.test"
    env.start()
    assert env.callback().status_code == 200


def test_invalid_config_is_403(env: Env) -> None:
    env.start()
    env.broken = True
    r = env.callback()
    assert r.status_code == 403 and r.text == "panel unavailable: kh.yaml is invalid"


def test_upstream_error_is_403(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    env.upstream.error = UpstreamError("bad_code")
    env.start()
    with caplog.at_level("WARNING"):
        r = env.callback()
    assert r.status_code == 403 and r.text == "sign-in refused"
    assert "bad_code" in caplog.text


def test_idp_error_and_missing_code_are_refused(env: Env) -> None:
    env.start()
    r = env.client.get("/auth/callback", params={"error": "access_denied", "state": env.state})
    assert r.status_code == 403 and r.text == "sign-in refused"
    env.start()
    r = env.client.get("/auth/callback", params={"state": env.state})
    assert r.status_code == 403 and r.text == "sign-in refused"


def test_current_admin_tracks_config_per_request(env: Env) -> None:
    env.start()
    env.callback()
    assert current_admin(env.request(), env.ctx) == "admin@example.test"
    env.admins = []
    assert current_admin(env.request(), env.ctx) is None
    env.admins = ["admin@example.test"]
    env.broken = True
    assert current_admin(env.request(), env.ctx) is None
    env.broken = False
    assert current_admin(env.request(), env.ctx) == "admin@example.test"
    env.clock += 8 * 3600 + 1
    assert current_admin(env.request(), env.ctx) is None


def test_current_admin_without_cookie_is_none(env: Env) -> None:
    assert current_admin(env.request(), env.ctx) is None


def test_dev_admin(tmp_path: Path) -> None:
    e = Env(tmp_path, public=None, dev_admin="Dev@Example.test")
    assert current_admin(e.request(), e.ctx) == "dev@example.test"


def test_logout_clears_cookie(env: Env) -> None:
    env.start()
    env.callback()
    r = env.client.post("/api/logout")
    assert r.status_code == 204
    assert "__Host-kh_panel=" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
