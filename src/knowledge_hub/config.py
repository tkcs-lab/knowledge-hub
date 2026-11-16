"""``kh.yaml`` model and loader. Every error text starts with ``config:``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from knowledge_hub.approvals import BOT_LOGINS


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


ENTITY_KIND_RE = re.compile(r"[a-z][a-z0-9_-]{0,15}")
GRIST_TABLE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
ENTITY_LABEL_RE = re.compile(r"[^\x00-\x1f\x7f]{1,40}")
RESERVED_ENTITY_KINDS = frozenset({"doc", "raw", "derived"})
MAX_ENTITY_KINDS = 32
LEGACY_ENTITY_KINDS: dict[str, tuple[str, str]] = {
    "prd": ("Products", "product"),
    "cmp": ("Components", "component"),
    "ver": ("Versions", "version"),
    "cus": ("Customers", "customer"),
}


class EntityKind(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str
    audiences: list[str]
    label: str | None = None
    reference_audiences: list[str] | None = None


def _default_entities() -> dict[str, EntityKind]:
    audiences = {"prd": ["all"], "cmp": ["all"], "ver": ["all"], "cus": ["contracts"]}
    return {
        kind: EntityKind(
            table=table,
            audiences=audiences[kind],
            label=label,
            reference_audiences=["contracts", "mgmt"] if kind == "cus" else None,
        )
        for kind, (table, label) in LEGACY_ENTITY_KINDS.items()
    }


TEAM_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
AUDIENCE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


class GovernanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_teams: dict[str, list[str]] = {}

    @field_validator("owner_teams")
    @classmethod
    def _team_names(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for teams in value.values():
            for team in teams:
                if not TEAM_RE.fullmatch(team):
                    raise ValueError(f"governance owner_teams team {team!r} is not a valid team name")
        return value


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audiences: list[str] = ["all", "mgmt", "hr", "contracts"]
    participant_label: str = "attendees"
    domains: list[str] = ["portfolio", "product", "technical"]
    repos: dict[str, RepoConfig] = _default_repos()
    raw_audience_map: dict[str, list[str]] = {}
    ingest_principal_types: list[str] = ["n8n_workflow"]
    restricted_audiences_imply_all: bool = False
    entities: dict[str, EntityKind] = _default_entities()
    forgejo_web_url: str | None = None
    governance: GovernanceConfig = GovernanceConfig()
    self_merge_teams: list[str] = []
    bot_logins: list[str] = ["kh-hub-bot"]

    @field_validator("entities", mode="before")
    @classmethod
    def _legacy_entity_lists(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        out: dict[Any, Any] = {}
        for kind, spec in value.items():
            if isinstance(spec, list):
                if kind not in LEGACY_ENTITY_KINDS:
                    raise ValueError(
                        f"entities {kind}: the list form is only for prd, cmp, ver and cus; "
                        "use {table, audiences}"
                    )
                table, label = LEGACY_ENTITY_KINDS[kind]
                spec = {"table": table, "audiences": spec, "label": label}
                if kind == "cus":
                    spec["reference_audiences"] = ["contracts", "mgmt"]
            out[kind] = spec
        return out

    @field_validator("self_merge_teams")
    @classmethod
    def _dedupe_self_merge_teams(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @property
    def bots(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((BOT_LOGINS[0], *self.bot_logins)))
    @field_validator("audiences")
    @classmethod
    def _audience_names(cls, value: list[str]) -> list[str]:
        for a in value:
            if not AUDIENCE_RE.fullmatch(a):
                raise ValueError(f"audience {a!r} is not a valid audience name")
        return value

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
        if not self.entities:
            raise ValueError("entities must name at least one kind")
        if len(self.entities) > MAX_ENTITY_KINDS:
            raise ValueError(f"entities has more than {MAX_ENTITY_KINDS} kinds")
        tables: dict[str, str] = {}
        for kind, spec in self.entities.items():
            if not ENTITY_KIND_RE.fullmatch(kind):
                raise ValueError(
                    f"entities kind {kind!r} must be 1-16 lowercase letters, digits, '_' or '-', "
                    "starting with a letter"
                )
            if kind in RESERVED_ENTITY_KINDS:
                raise ValueError(f"entities kind {kind!r} is reserved")
            if not GRIST_TABLE_RE.fullmatch(spec.table):
                raise ValueError(
                    f"entities {kind} table {spec.table!r} must be letters, digits or '_', "
                    "starting with a letter"
                )
            if spec.table in tables:
                raise ValueError(
                    f"entities {tables[spec.table]} and {kind} use the same table {spec.table!r}"
                )
            tables[spec.table] = kind
            if spec.label is not None and not ENTITY_LABEL_RE.fullmatch(spec.label):
                raise ValueError(f"entities {kind} label must be 1-40 characters without control characters")
            for a in spec.audiences:
                if a not in known:
                    raise ValueError(f"entities {kind} has unknown audience {a!r}")
            for a in spec.reference_audiences or []:
                if a not in known:
                    raise ValueError(f"entities {kind} has unknown reference audience {a!r}")
        for a in self.governance.owner_teams:
            if a not in known:
                raise ValueError(f"governance owner_teams has unknown audience {a!r}")
        for t in self.self_merge_teams:
            if not TEAM_RE.fullmatch(t):
                raise ValueError(f"self_merge_teams team {t!r} is not a valid team name")
        for b in self.bot_logins:
            if not LOGIN_RE.fullmatch(b):
                raise ValueError(f"bot_logins login {b!r} is not a valid login")
        for p in self.ingest_principal_types:
            if p != "n8n_workflow":
                raise ValueError(f"ingest_principal_types value {p!r} is not allowed; only n8n_workflow")
        return self


def owner_teams_for(config: Config, audience: str) -> list[str]:
    configured = config.governance.owner_teams.get(audience)
    if configured:
        return list(configured)
    if audience == "all":
        return ["owners-portfolio", "owners-product", "owners-technical"]
    return [f"owners-{audience}"]


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
