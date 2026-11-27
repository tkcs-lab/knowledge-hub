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
    assert s.bi_enabled is False
    assert s.review_overdue_half_life_days == 0 and s.review_overdue_floor == 0.5


def test_overdue_settings_are_bounded(monkeypatch):
    for name, value in (
        ("KH_REVIEW_OVERDUE_HALF_LIFE_DAYS", "-1"),
        ("KH_REVIEW_OVERDUE_HALF_LIFE_DAYS", "36501"),
        ("KH_REVIEW_OVERDUE_FLOOR", "-0.1"),
        ("KH_REVIEW_OVERDUE_FLOOR", "1.1"),
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValidationError):
            Settings()
        monkeypatch.delenv(name)


def test_record_purge_days(monkeypatch):
    assert Settings().record_purge_days == 30
    for bad in ("0", "3651"):
        monkeypatch.setenv("KH_RECORD_PURGE_DAYS", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_usage_settings(monkeypatch):
    s = Settings()
    assert s.usage_log is True and s.usage_retention_days == 180
    assert s.query_timeout_ms == 3000 and s.query_max_rows == 1000
    monkeypatch.setenv("KH_USAGE_LOG", "false")
    assert Settings().usage_log is False
    for bad in ("0", "3651"):
        monkeypatch.setenv("KH_USAGE_RETENTION_DAYS", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_query_settings_refuse_5001(monkeypatch):
    for var in ("KH_QUERY_TIMEOUT_MS", "KH_QUERY_MAX_ROWS"):
        monkeypatch.setenv(var, "5001")
        with pytest.raises(ValidationError):
            Settings()
        monkeypatch.delenv(var)


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
    assert s.tier_weights == "T0:1.0,T0a:0.7,T3:0.4"
    assert s.max_record_bytes == 1048576
    for gone in (
        "forgejo_org", "forgejo_notes_token_file", "agent_notes_enabled", "agent_proposals_enabled",
        "agent_email_domains", "agent_note_daily_limit", "agent_proposal_daily_limit",
        "agent_max_body_bytes", "ci_status_logins", "promotion_daily_limit", "superseded_weight",
        "disputed_weight", "grist_url", "grist_doc_id", "grist_key_file", "max_raw_bytes",
        "pf_issuer", "pf_audience", "pf_jwks_url",
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


def test_search_min_similarity_setting(monkeypatch):
    assert Settings().search_min_similarity == 0.0
    monkeypatch.setenv("KH_SEARCH_MIN_SIMILARITY", "0.35")
    assert Settings().search_min_similarity == 0.35
    for bad in ("-0.1", "1.5"):
        monkeypatch.setenv("KH_SEARCH_MIN_SIMILARITY", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_tier_weights_without_t0a_gets_the_midpoint():
    from knowledge_hub.search.sql import parse_tier_weights

    assert parse_tier_weights("T0:1.0,T3:0.4") == {"T0": 1.0, "T0a": pytest.approx(0.7), "T3": 0.4}
    assert Settings(tier_weights="T0:1.0,T3:0.4").tier_weights == "T0:1.0,T3:0.4"


@pytest.mark.parametrize(
    "bad",
    ["T0:1.0,T1:0.5,T3:0.4", "T0:1.0,T0a:0.7,T3:0.4,T9:0.1"],
)
def test_tier_weights_reject_unknown_tier(bad):
    with pytest.raises(ValidationError, match="KH_TIER_WEIGHTS must give a positive weight"):
        Settings(tier_weights=bad)


def test_tier_weights_reject_t0a_above_t0():
    with pytest.raises(ValidationError, match="T0 >= T0a >= T3"):
        Settings(tier_weights="T0:1.0,T0a:1.1,T3:0.4")


def test_tier_weights_reject_t0a_below_t3():
    with pytest.raises(ValidationError, match="T0 >= T0a >= T3"):
        Settings(tier_weights="T0:1.0,T0a:0.3,T3:0.4")


def test_tech_poll_minutes_defaults_to_60(monkeypatch):
    assert Settings().tech_poll_minutes == 60
    monkeypatch.setenv("KH_TECH_POLL_MINUTES", "0")
    assert Settings().tech_poll_minutes == 0
    for bad in ("-1", "10081"):
        monkeypatch.setenv("KH_TECH_POLL_MINUTES", bad)
        with pytest.raises(ValidationError):
            Settings()


OAUTH = {
    "KH_PUBLIC_URL": "https://kh.example.com",
    "KH_OIDC_ISSUER": "https://sso.example.com",
    "KH_OIDC_CLIENT_ID": "kh",
    "KH_OIDC_CLIENT_SECRET_FILE": "/run/secrets/oidc",
    "KH_OIDC_ALLOWED_DOMAINS": "Example.com, ,b.io",
}


def _oauth(monkeypatch, **over):
    for k, v in {**OAUTH, **over}.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def test_oauth_defaults_and_properties(monkeypatch):
    s = Settings()
    assert s.oauth_enabled is False and s.resource_url is None
    assert s.oauth_dcr is True and s.directory_view_seconds == 30
    assert s.oauth_access_ttl_seconds == 3600 and s.oauth_refresh_ttl_days == 30
    assert s.oauth_max_clients == 200 and s.oauth_register_per_min == 30 and s.oauth_authorize_per_min == 600
    assert s.directory_keycloak_subgroups is False
    assert s.redirect_allowlist_tuple == ("https://claude.ai/api/mcp/auth_callback", "http://localhost", "http://127.0.0.1")
    _oauth(monkeypatch)
    s = Settings()
    assert s.oauth_enabled and s.resource_url == "https://kh.example.com/mcp"
    assert s.allowed_domains_tuple == ("example.com", "b.io") and s.hosted_domains_tuple == ()


def test_oauth_numeric_bounds(monkeypatch):
    for name, bad in (
        ("OAUTH_ACCESS_TTL_SECONDS", "299"), ("OAUTH_ACCESS_TTL_SECONDS", "86401"),
        ("OAUTH_REFRESH_TTL_DAYS", "0"), ("OAUTH_REFRESH_TTL_DAYS", "366"),
        ("OAUTH_MAX_CLIENTS", "0"), ("OAUTH_MAX_CLIENTS", "10001"),
        ("OAUTH_REGISTER_PER_MIN", "1001"), ("OAUTH_AUTHORIZE_PER_MIN", "100001"),
        ("DIRECTORY_VIEW_SECONDS", "4"), ("DIRECTORY_VIEW_SECONDS", "601"),
    ):
        monkeypatch.setenv(f"KH_{name}", bad)
        with pytest.raises(ValidationError):
            Settings()
        monkeypatch.delenv(f"KH_{name}")


@pytest.mark.parametrize(
    "missing", ["KH_PUBLIC_URL", "KH_OIDC_ISSUER", "KH_OIDC_CLIENT_ID", "KH_OIDC_CLIENT_SECRET_FILE"]
)
def test_oidc_must_be_set_together(monkeypatch, missing):
    _oauth(monkeypatch, **{missing: None})
    with pytest.raises(ValidationError, match="KH_OIDC_\\* and KH_PUBLIC_URL must be set together"):
        Settings()


def test_oauth_requires_allowed_domains(monkeypatch):
    _oauth(monkeypatch, KH_OIDC_ALLOWED_DOMAINS=" , ")
    with pytest.raises(ValidationError, match="KH_OIDC_ALLOWED_DOMAINS is required when OAuth is on"):
        Settings()


def test_google_issuer_requires_hosted_domains(monkeypatch):
    _oauth(monkeypatch, KH_OIDC_ISSUER="https://accounts.google.com")
    with pytest.raises(ValidationError, match="KH_OIDC_HOSTED_DOMAINS is required for a Google issuer"):
        Settings()
    monkeypatch.setenv("KH_OIDC_HOSTED_DOMAINS", "Example.com")
    assert Settings().hosted_domains_tuple == ("example.com",)


def test_dev_identity_refused_while_oauth_is_on(monkeypatch):
    _oauth(monkeypatch, KH_ENV="dev", KH_DEV_IDENTITY=IDENT)
    with pytest.raises(ValidationError, match="KH_DEV_IDENTITY must not be set while OAuth is on"):
        Settings()


@pytest.mark.parametrize(
    ("url", "env", "ok"),
    [
        ("https://h", "prod", True),
        ("https://h:8443", "prod", True),
        ("https://h/", "prod", False),
        ("https://h/x", "prod", False),
        ("https://u@h", "prod", False),
        ("https://h?q", "prod", False),
        ("http://h", "dev", False),
        ("http://localhost", "prod", False),
        ("http://localhost:8080", "test", True),
        ("http://127.0.0.1:8080", "dev", True),
        ("http://[::1]:8080", "dev", True),
    ],
)
def test_public_url_shapes(monkeypatch, url, env, ok):
    _oauth(monkeypatch, KH_PUBLIC_URL=url, KH_ENV=env)
    if ok:
        assert Settings().public_url == url
    else:
        with pytest.raises(ValidationError, match="KH_PUBLIC_URL must be an https URL without a path"):
            Settings()


@pytest.mark.parametrize("value", ["0", "3600"])
def test_config_watch_seconds_bounds(monkeypatch, value):
    monkeypatch.setenv("KH_CONFIG_WATCH_SECONDS", value)
    assert load_settings().config_watch_seconds == int(value)


def test_config_watch_seconds_default_and_limit(monkeypatch):
    assert load_settings().config_watch_seconds == 10
    monkeypatch.setenv("KH_CONFIG_WATCH_SECONDS", "3601")
    with pytest.raises(ValidationError):
        load_settings()


def test_public_url_http_still_refused(monkeypatch):
    monkeypatch.setenv("KH_PUBLIC_URL", "http://x.test")
    monkeypatch.setenv("KH_OIDC_ISSUER", "https://i.test")
    monkeypatch.setenv("KH_OIDC_CLIENT_ID", "c")
    monkeypatch.setenv("KH_OIDC_CLIENT_SECRET_FILE", "/s")
    with pytest.raises(ValidationError, match="KH_PUBLIC_URL must be an https URL without a path"):
        load_settings()
