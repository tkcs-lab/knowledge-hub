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
    assert s.s3_region == "garage"
    assert s.rrf_k == 60 and s.search_pool == 50
    assert s.dev_identity is None and s.db_app_password_file is None


def test_usage_settings(monkeypatch):
    s = Settings()
    assert s.usage_log is True and s.usage_retention_days == 180
    monkeypatch.setenv("KH_USAGE_LOG", "false")
    assert Settings().usage_log is False
    for bad in ("0", "3651"):
        monkeypatch.setenv("KH_USAGE_RETENTION_DAYS", bad)
        with pytest.raises(ValidationError):
            Settings()


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


def test_changed_and_removed_settings(monkeypatch):
    s = load_settings()
    assert s.tier_weights == "T0:1.0,T3:0.4"
    assert s.max_record_bytes == 1048576
    for gone in (
        "forgejo_org", "forgejo_notes_token_file", "agent_notes_enabled", "agent_proposals_enabled",
        "agent_email_domains", "agent_note_daily_limit", "agent_proposal_daily_limit",
        "agent_max_body_bytes", "ci_status_logins", "promotion_daily_limit", "superseded_weight",
        "disputed_weight", "grist_url", "grist_doc_id", "grist_key_file", "max_raw_bytes",
    ):
        assert gone not in Settings.model_fields, gone
    monkeypatch.setenv("KH_MAX_RECORD_BYTES", "10")
    assert load_settings().max_record_bytes == 10


def test_cli_has_only_serve_and_migrate():
    assert cli.COMMANDS == ("serve", "migrate")


def test_similar_settings(monkeypatch):
    s = Settings()
    assert s.similar_limit == 3 and s.similar_min_score == 0.5
    monkeypatch.setenv("KH_SIMILAR_LIMIT", "5")
    monkeypatch.setenv("KH_SIMILAR_MIN_SCORE", "0.8")
    s = Settings()
    assert s.similar_limit == 5 and s.similar_min_score == 0.8
    for name, bad in (("KH_SIMILAR_LIMIT", "11"), ("KH_SIMILAR_MIN_SCORE", "1.5")):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValidationError):
            Settings()
        monkeypatch.undo()
