"""``kh.yaml`` model and loader. Every error text starts with ``config:``."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


class ConfigError(ValueError):
    pass


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: Literal["T0", "T1"]
    audience: str
    trust_anchor: str | None = None


def _default_repos() -> dict[str, RepoConfig]:
    repos = {
        f"canonical-{a}": RepoConfig(tier="T0", audience=a) for a in ("all", "mgmt", "hr", "contracts")
    }
    repos["notes-all"] = RepoConfig(tier="T1", audience="all")
    return repos


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audiences: list[str] = ["all", "mgmt", "hr", "contracts"]
    participant_label: str = "attendees"
    domains: list[str] = ["portfolio", "product", "technical"]
    repos: dict[str, RepoConfig] = _default_repos()
    raw_audience_map: dict[str, list[str]] = {}
    ingest_principal_types: list[str] = ["n8n_workflow"]
    restricted_audiences_imply_all: bool = False
    entities: dict[Literal["prd", "cmp", "ver", "cus"], list[str]] = {
        "prd": ["all"],
        "cmp": ["all"],
        "ver": ["all"],
        "cus": ["contracts"],
    }
    forgejo_web_url: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> Config:
        known = set(self.audiences)
        for name, repo in self.repos.items():
            if repo.audience not in known:
                raise ValueError(f"repo {name} audience {repo.audience!r} is not in audiences")
        t0: dict[str, str] = {}
        for name, repo in self.repos.items():
            if repo.tier == "T0":
                if repo.audience in t0:
                    raise ValueError(
                        f"audience {repo.audience!r} has more than one T0 repo ({t0[repo.audience]}, {name})"
                    )
                t0[repo.audience] = name
        for key, auds in self.raw_audience_map.items():
            for a in auds:
                if a not in known:
                    raise ValueError(f"raw_audience_map {key} has unknown audience {a!r}")
        for kind, auds in self.entities.items():
            for a in auds:
                if a not in known:
                    raise ValueError(f"entities {kind} has unknown audience {a!r}")
        for p in self.ingest_principal_types:
            if p != "n8n_workflow":
                raise ValueError(f"ingest_principal_types value {p!r} is not allowed; only n8n_workflow")
        return self


def _wrap(exc: ValidationError) -> ConfigError:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"])
    msg = first["msg"].removeprefix("Value error, ")
    return ConfigError(f"config: {msg}" + (f" ({where})" if where else ""))


def parse_config(data: dict | None) -> Config:
    try:
        return Config.model_validate(data or {})
    except ValidationError as exc:
        raise _wrap(exc) from exc


def load_config(path: str | Path | None = None) -> Config:
    if path is None:
        return parse_config(None)
    try:
        data = yaml.safe_load(Path(path).read_text())
    except OSError as exc:
        raise ConfigError(f"config: cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"config: invalid YAML in {path}") from exc
    if data is not None and not isinstance(data, dict):
        raise ConfigError("config: top level must be a mapping")
    return parse_config(data)
