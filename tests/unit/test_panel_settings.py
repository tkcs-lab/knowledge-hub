import os

import pytest
from pydantic import ValidationError

from knowledge_hub.panel.settings import PanelSettings
from knowledge_hub.settings import Settings

pytestmark = pytest.mark.unit


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("KH_"):
            monkeypatch.delenv(k)
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32 + b"\n")
    full = {
        "KH_CONFIG_FILE": str(tmp_path / "kh.yaml"),
        "KH_PANEL_PUBLIC_URL": "https://panel.example.com",
        "KH_PANEL_OIDC_ISSUER": "https://idp.example.com",
        "KH_PANEL_OIDC_CLIENT_ID": "cid",
        "KH_PANEL_OIDC_CLIENT_SECRET_FILE": str(tmp_path / "secret"),
        "KH_PANEL_OIDC_ALLOWED_DOMAINS": "example.com",
        "KH_PANEL_SESSION_KEY_FILE": str(key),
    }

    def apply(**over):
        data = {**full, **over}
        for k, v in data.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)

    apply()
    return apply, tmp_path


def test_panel_environment_loads_both(env):
    s = PanelSettings()
    assert s.session_hours == 8 and s.hub_url == "http://knowledge-hub:8080"
    assert s.allowed_domains_tuple == ("example.com",)
    assert s.read_session_key() == b"k" * 32
    Settings()


@pytest.mark.parametrize(
    "name",
    [
        "KH_CONFIG_FILE",
        "KH_PANEL_PUBLIC_URL",
        "KH_PANEL_OIDC_ISSUER",
        "KH_PANEL_OIDC_CLIENT_ID",
        "KH_PANEL_OIDC_CLIENT_SECRET_FILE",
        "KH_PANEL_OIDC_ALLOWED_DOMAINS",
        "KH_PANEL_SESSION_KEY_FILE",
    ],
)
def test_missing_variable(env, name):
    apply, _ = env
    apply(**{name: None})
    with pytest.raises(ValidationError, match=f"{name} is required"):
        PanelSettings()


def test_google_needs_hosted_domains(env):
    apply, _ = env
    apply(KH_PANEL_OIDC_ISSUER="https://accounts.google.com")
    with pytest.raises(ValidationError, match="KH_PANEL_OIDC_HOSTED_DOMAINS is required for a Google issuer"):
        PanelSettings()
    apply(KH_PANEL_OIDC_ISSUER="https://accounts.google.com", KH_PANEL_OIDC_HOSTED_DOMAINS="example.com")
    assert PanelSettings().hosted_domains_tuple == ("example.com",)


def test_public_url_rule(env):
    apply, _ = env
    apply(KH_PANEL_PUBLIC_URL="http://panel.example.com")
    with pytest.raises(ValidationError, match="KH_PANEL_PUBLIC_URL must be an https URL without a path"):
        PanelSettings()


def test_dev_admin(env):
    apply, _ = env
    apply(KH_PANEL_DEV_ADMIN="Me@X.io")
    msg = "KH_PANEL_DEV_ADMIN is only allowed when KH_ENV is dev or test"
    with pytest.raises(ValidationError, match=msg):
        PanelSettings()
    apply(
        KH_ENV="dev",
        KH_PANEL_DEV_ADMIN="Me@X.io",
        KH_PANEL_PUBLIC_URL=None,
        KH_PANEL_OIDC_ISSUER=None,
        KH_PANEL_OIDC_CLIENT_ID=None,
        KH_PANEL_OIDC_CLIENT_SECRET_FILE=None,
        KH_PANEL_OIDC_ALLOWED_DOMAINS=None,
        KH_PANEL_SESSION_KEY_FILE=None,
    )
    assert PanelSettings().dev_admin == "me@x.io"


def test_short_session_key(env):
    _, tmp = env
    (tmp / "key").write_bytes(b"k" * 31 + b"\n")
    with pytest.raises(ValueError, match="at least 32 bytes"):
        PanelSettings().read_session_key()
