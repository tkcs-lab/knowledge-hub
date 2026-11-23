"""Environment settings (prefix ``KH_``). Secrets are read from files, never from variables."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from knowledge_hub.search.sql import parse_tier_weights


class DevIdentity(BaseModel):
    sub: str
    principal_type: Literal["claude_client", "n8n_workflow"]
    audiences: list[str] = []
    email: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KH_", extra="ignore")

    env: Literal["dev", "test", "prod"] = "prod"
    dev_identity: DevIdentity | None = None
    allowed_hosts: str = ""
    max_body_bytes: int = 1048576

    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "kh"
    db_migrator_password_file: Path | None = None
    db_app_password_file: Path | None = None
    db_indexer_password_file: Path | None = None

    config_file: Path | None = None
    forgejo_url: str | None = None
    forgejo_bot_token_file: Path | None = None
    forgejo_indexer_token_file: Path | None = None
    tech_poll_minutes: int = Field(60, ge=0, le=10080)
    webhook_hmac_file: Path | None = None
    config_watch_seconds: int = Field(10, ge=0, le=3600)

    embed_provider: Literal["ollama", "http", "fake"] = "ollama"
    ollama_url: str | None = None
    embed_model: str = "bge-m3"
    embed_dim: int = 1024
    embed_http_url: str | None = None
    embed_http_token_file: Path | None = None
    embed_http_allowed_audiences: str = "all"

    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_access_key_file: Path | None = None
    s3_secret_key_file: Path | None = None
    s3_region: str = "garage"

    cursor_key_file: Path | None = None
    chunker_version: int = 1
    rrf_k: int = 60
    search_pool: int = 50
    search_min_similarity: float = Field(0.0, ge=0.0, le=1.0)
    tier_weights: str = "T0:1.0,T0a:0.7,T3:0.4"
    stale_weight: float = 0.5
    similar_limit: int = Field(3, ge=0, le=10)
    similar_min_score: float = Field(0.5, ge=0.0, le=1.0)
    search_max_offset: int = 100
    max_record_bytes: int = 1048576
    propose_max_bytes: int = 131072
    propose_max_open: int = 20
    rate_limit_per_min: int = 120
    metrics_token_file: Path | None = None
    usage_log: bool = True
    usage_retention_days: int = Field(default=180, ge=1, le=3650)
    query_timeout_ms: int = Field(3000, ge=100, le=5000)
    query_max_rows: int = Field(1000, ge=1, le=5000)

    public_url: str | None = None
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret_file: Path | None = None
    oidc_allowed_domains: str = ""
    oidc_hosted_domains: str = ""
    oauth_dcr: bool = True
    oauth_redirect_allowlist: str = (
        "https://claude.ai/api/mcp/auth_callback,http://localhost,http://127.0.0.1"
    )
    oauth_access_ttl_seconds: int = Field(3600, ge=300, le=86400)
    oauth_refresh_ttl_days: int = Field(30, ge=1, le=365)
    oauth_max_clients: int = Field(200, ge=1, le=10000)
    oauth_register_per_min: int = Field(30, ge=1, le=1000)
    oauth_authorize_per_min: int = Field(600, ge=1, le=100000)

    directory_view_seconds: int = Field(30, ge=5, le=600)
    directory_google_credentials_file: Path | None = None
    directory_google_subject: str | None = None
    directory_keycloak_url: str | None = None
    directory_keycloak_realm: str | None = None
    directory_keycloak_client_id: str | None = None
    directory_keycloak_client_secret_file: Path | None = None
    directory_keycloak_subgroups: bool = False

    setup_pg_admin_dsn_file: Path | None = None
    setup_forgejo_admin_token_file: Path | None = None
    setup_slack_webhooks_file: Path | None = None

    @field_validator("dev_identity", mode="before")
    @classmethod
    def _parse_dev_identity(cls, v):
        if isinstance(v, str):
            if not v.strip():
                return None
            try:
                return json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError(f"KH_DEV_IDENTITY is not valid JSON: {exc}") from exc
        return v

    @field_validator("tier_weights")
    @classmethod
    def _check_tier_weights(cls, v: str) -> str:
        parse_tier_weights(v)
        return v

    @model_validator(mode="after")
    def _check_env_rules(self) -> Settings:
        if self.env == "prod" and self.dev_identity is not None:
            raise ValueError("KH_DEV_IDENTITY is set while KH_ENV=prod; refusing to start")
        if self.embed_provider == "fake" and self.env not in ("dev", "test"):
            raise ValueError("KH_EMBED_PROVIDER=fake is only allowed when KH_ENV is dev or test")
        oidc = (self.public_url, self.oidc_issuer, self.oidc_client_id, self.oidc_client_secret_file)
        if any(v is not None and v != "" for v in oidc) and not all(v is not None and v != "" for v in oidc):
            raise ValueError("KH_OIDC_* and KH_PUBLIC_URL must be set together")
        if self.public_url:
            self._check_public_url(self.public_url)
        if self.oauth_enabled:
            if not self.allowed_domains_tuple:
                raise ValueError("KH_OIDC_ALLOWED_DOMAINS is required when OAuth is on")
            issuer_host = (urlsplit(self.oidc_issuer or "").hostname or "").lower()
            if issuer_host == "accounts.google.com" and not self.hosted_domains_tuple:
                raise ValueError("KH_OIDC_HOSTED_DOMAINS is required for a Google issuer")
            if self.dev_identity is not None:
                raise ValueError("KH_DEV_IDENTITY must not be set while OAuth is on")
        return self

    def _check_public_url(self, url: str) -> None:
        check_public_url(url, self.env, "KH_PUBLIC_URL")

    @property
    def oauth_enabled(self) -> bool:
        return all(
            v is not None and v != ""
            for v in (self.public_url, self.oidc_issuer, self.oidc_client_id, self.oidc_client_secret_file)
        )

    @property
    def resource_url(self) -> str | None:
        return f"{self.public_url}/mcp" if self.public_url else None

    @property
    def allowed_domains_tuple(self) -> tuple[str, ...]:
        return _csv(self.oidc_allowed_domains)

    @property
    def hosted_domains_tuple(self) -> tuple[str, ...]:
        return _csv(self.oidc_hosted_domains)

    @property
    def redirect_allowlist_tuple(self) -> tuple[str, ...]:
        return _csv(self.oauth_redirect_allowlist)

    def dsn(self, role: Literal["migrator", "app", "indexer"]) -> str:
        """Build a libpq key/value DSN for ``kh_<role>``."""
        password_file = {
            "migrator": self.db_migrator_password_file,
            "app": self.db_app_password_file,
            "indexer": self.db_indexer_password_file,
        }[role]
        parts = {
            "host": self.db_host,
            "port": str(self.db_port),
            "dbname": self.db_name,
            "user": f"kh_{role}",
        }
        if password_file is not None:
            parts["password"] = password_file.read_text().strip()
        return " ".join(f"{k}={_quote(v)}" for k, v in parts.items())


def check_public_url(url: str, env: str, name: str) -> None:
    msg = f"{name} must be an https URL without a path"
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError as exc:
        raise ValueError(msg) from exc
    if not host or parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError(msg)
    if parts.path or parts.query or parts.fragment or url.endswith(("/", "?", "#")):
        raise ValueError(msg)
    if parts.scheme == "https":
        return
    loopback = host in ("localhost", "127.0.0.1", "::1")
    if parts.scheme == "http" and loopback and env in ("dev", "test"):
        return
    raise ValueError(msg)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(p for p in (x.strip().lower() for x in value.split(",")) if p)


def _quote(value: str) -> str:
    if value and not any(c in value for c in " '\\"):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def load_settings() -> Settings:
    return Settings()
