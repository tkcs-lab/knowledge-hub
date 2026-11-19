"""Panel environment settings (prefix ``KH_PANEL_``; ``KH_ENV`` and ``KH_CONFIG_FILE`` are shared)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from knowledge_hub.settings import _csv, check_public_url


class PanelSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KH_PANEL_", extra="ignore", populate_by_name=True)

    env: Literal["dev", "test", "prod"] = Field("prod", validation_alias="KH_ENV")
    config_file: Path | None = Field(None, validation_alias="KH_CONFIG_FILE")
    public_url: str | None = None
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret_file: Path | None = None
    oidc_allowed_domains: str = ""
    oidc_hosted_domains: str = ""
    session_key_file: Path | None = None
    session_hours: int = Field(8, ge=1, le=24)
    hub_url: str = "http://knowledge-hub:8080"
    dev_admin: str | None = None

    @model_validator(mode="after")
    def _check_rules(self) -> PanelSettings:
        if self.dev_admin is not None and self.env not in ("dev", "test"):
            raise ValueError("KH_PANEL_DEV_ADMIN is only allowed when KH_ENV is dev or test")
        if self.config_file is None:
            raise ValueError("KH_CONFIG_FILE is required")
        if self.dev_admin is not None:
            self.dev_admin = self.dev_admin.strip().lower()
            if self.public_url:
                check_public_url(self.public_url, self.env, "KH_PANEL_PUBLIC_URL")
            return self
        required = (
            ("KH_PANEL_PUBLIC_URL", self.public_url),
            ("KH_PANEL_OIDC_ISSUER", self.oidc_issuer),
            ("KH_PANEL_OIDC_CLIENT_ID", self.oidc_client_id),
            ("KH_PANEL_OIDC_CLIENT_SECRET_FILE", self.oidc_client_secret_file),
            ("KH_PANEL_OIDC_ALLOWED_DOMAINS", self.oidc_allowed_domains),
            ("KH_PANEL_SESSION_KEY_FILE", self.session_key_file),
        )
        for name, value in required:
            if value is None or value == "":
                raise ValueError(f"{name} is required")
        check_public_url(self.public_url or "", self.env, "KH_PANEL_PUBLIC_URL")
        if not self.allowed_domains_tuple:
            raise ValueError("KH_PANEL_OIDC_ALLOWED_DOMAINS is required")
        issuer_host = (urlsplit(self.oidc_issuer or "").hostname or "").lower()
        if issuer_host == "accounts.google.com" and not self.hosted_domains_tuple:
            raise ValueError("KH_PANEL_OIDC_HOSTED_DOMAINS is required for a Google issuer")
        return self

    @property
    def allowed_domains_tuple(self) -> tuple[str, ...]:
        return _csv(self.oidc_allowed_domains)

    @property
    def hosted_domains_tuple(self) -> tuple[str, ...]:
        return _csv(self.oidc_hosted_domains)

    def read_session_key(self) -> bytes:
        if self.session_key_file is None:
            raise ValueError("KH_PANEL_SESSION_KEY_FILE is required")
        key = self.session_key_file.read_bytes().strip()
        if len(key) < 32:
            raise ValueError("KH_PANEL_SESSION_KEY_FILE must hold at least 32 bytes")
        return key
