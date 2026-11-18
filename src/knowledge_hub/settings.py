"""Environment settings (prefix ``KH_``). Secrets are read from files, never from variables."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

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
    pf_issuer: str | None = None
    pf_audience: str | None = None
    pf_jwks_url: str | None = None
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

    setup_pg_admin_dsn_file: Path | None = None
    setup_forgejo_admin_token_file: Path | None = None

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
        return self

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


def _quote(value: str) -> str:
    if value and not any(c in value for c in " '\\"):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def load_settings() -> Settings:
    return Settings()
