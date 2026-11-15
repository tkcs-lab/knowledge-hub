import json

import pytest
from pydantic import ValidationError

from knowledge_hub import cli
from knowledge_hub.settings import Settings, load_settings

pytestmark = pytest.mark.unit

IDENT = json.dumps({"sub": "dev", "principal_type": "claude_client", "audiences": ["all"]})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith("KH_"):
            monkeypatch.delenv(k)


def test_defaults():
    s = load_settings()
    assert s.env == "prod"
    assert s.db_host == "postgres" and s.db_port == 5432 and s.db_name == "kh"
    assert s.max_body_bytes == 1048576
    assert s.embed_model == "bge-m3" and s.embed_dim == 1024
    assert s.s3_region == "garage" and s.forgejo_org == "kh"
    assert s.rrf_k == 60 and s.search_pool == 50 and s.promotion_daily_limit == 5
    assert s.dev_identity is None and s.db_app_password_file is None


def test_prod_refuses_dev_identity(monkeypatch):
    monkeypatch.setenv("KH_DEV_IDENTITY", IDENT)
    with pytest.raises(ValidationError, match="KH_DEV_IDENTITY is set while KH_ENV=prod; refusing to start"):
        Settings()


def test_fake_provider_refused_in_prod(monkeypatch):
    monkeypatch.setenv("KH_EMBED_PROVIDER", "fake")
    msg = "KH_EMBED_PROVIDER=fake is only allowed when KH_ENV is dev or test"
    with pytest.raises(ValidationError, match=msg):
        Settings()


def test_dev_identity_accepted_in_dev(monkeypatch):
    monkeypatch.setenv("KH_ENV", "dev")
    monkeypatch.setenv("KH_DEV_IDENTITY", IDENT)
    monkeypatch.setenv("KH_EMBED_PROVIDER", "fake")
    s = Settings()
    assert s.dev_identity.sub == "dev" and s.dev_identity.audiences == ["all"]


def test_dev_identity_refuses_service(monkeypatch):
    monkeypatch.setenv("KH_ENV", "dev")
    monkeypatch.setenv("KH_DEV_IDENTITY", json.dumps({"sub": "x", "principal_type": "service"}))
    with pytest.raises(ValidationError):
        Settings()


def test_dsn_socket_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("KH_DB_HOST", "/var/run/postgresql")
    assert Settings().dsn("app") == "host=/var/run/postgresql port=5432 dbname=kh user=kh_app"
    pw = tmp_path / "pw"
    pw.write_text("s3cret\n")
    monkeypatch.setenv("KH_DB_APP_PASSWORD_FILE", str(pw))
    assert Settings().dsn("app").endswith("user=kh_app password=s3cret")


def test_cli_serve_exits_2_on_settings_error(monkeypatch, capsys):
    monkeypatch.setenv("KH_ENV", "prod")
    monkeypatch.setenv("KH_DEV_IDENTITY", IDENT)
    assert cli.main(["serve"]) == 2
    assert "refusing to start" in capsys.readouterr().err


def test_cli_stub_exits_3(capsys):
    assert cli.main(["migrate"]) == 3
    assert "not implemented yet" in capsys.readouterr().out
