"""``kh.yaml`` model and loader (plan 3.4). Pure, no I/O except ``load_config``.

Every error text starts with ``config:``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pydantic_core
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigError(ValueError):
    pass


AUDIENCE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
GROUP_RE = AUDIENCE_RE
LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,38}")
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,62}")
REPO_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")
DIR_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99})*")
SLACK_CHANNEL_RE = re.compile(r"#[a-z0-9][a-z0-9_-]{0,79}")

TECH_AUDIENCE = "all"

MAX_LABELS = 500
MAX_TECH_DIRS = 20
MAX_LABEL_DESCRIPTION = 200
MAX_EMAIL = 254
MAX_AUDIENCES = 200
MAX_AUDIENCE_ADDRESSES = 20000
MAX_AUDIENCE_ANSWER_CHARS = 1_000_000


def _audience_table(people: dict[str, str], groups: dict[str, Any]) -> dict[str, list[str]]:
    """Active group name -> sorted, de-duplicated member emails (the PrivacyFence audiences answer)."""
    by_login = {login.casefold(): email.lower() for login, email in people.items()}
    return {
        name: sorted({by_login[m.casefold()] for m in group.members if m.casefold() in by_login})
        for name, group in sorted(groups.items())
        if not group.archived
    }


def _check_logins(logins: list[str], where: str) -> None:
    seen: set[str] = set()
    for login in logins:
        if not LOGIN_RE.fullmatch(login):
            raise ValueError(f"{where} login {login!r} is not a valid login")
        folded = login.casefold()
        if folded in seen:
            raise ValueError(f"{where} login {login!r} is listed twice")
        seen.add(folded)


class GroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str | None = None
    members: list[str] = []
    approvers: list[str]
    archived: bool = False
    slack_channel: str | None = None

    @field_validator("repo")
    @classmethod
    def _repo_name(cls, value: str | None) -> str | None:
        if value is not None and not REPO_RE.fullmatch(value):
            raise ValueError(f"repo {value!r} is not a valid repository name")
        return value

    @field_validator("slack_channel")
    @classmethod
    def _slack_channel(cls, value: str | None) -> str | None:
        if value is not None and not SLACK_CHANNEL_RE.fullmatch(value):
            raise ValueError(f"slack_channel {value!r} is not a valid Slack channel (#name, lower case)")
        return value


class TechRepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labels: list[str] = []
    dirs: list[str] = Field(default_factory=lambda: ["docs"])


class KhConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org: str = "kh"
    bot_login: str = "kh-bot"
    indexer_login: str = "kh-indexer"
    forgejo_web_url: str | None = None
    hub_url: str | None = None
    people: dict[str, str] = Field(default_factory=dict)
    groups: dict[str, GroupConfig] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    archived_labels: list[str] = Field(default_factory=list)
    tech_org: str | None = None
    tech_repos: dict[str, TechRepoConfig] = Field(default_factory=dict)

    @field_validator("forgejo_web_url")
    @classmethod
    def _web_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("forgejo_web_url must be null or an https URL")
        return value

    @field_validator("hub_url")
    @classmethod
    def _hub_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/"):
            raise ValueError("hub_url must be null or an http(s) URL without a path")
        if parts.query or parts.fragment:
            raise ValueError("hub_url must be null or an http(s) URL without a path")
        return value

    @model_validator(mode="after")
    def _validate(self) -> KhConfig:
        _check_logins([self.bot_login], "bot_login")
        _check_logins([self.indexer_login], "indexer_login")
        if self.bot_login.casefold() == self.indexer_login.casefold():
            raise ValueError("bot_login and indexer_login must differ")
        bots = {self.bot_login.casefold(), self.indexer_login.casefold()}

        folded_people: dict[str, str] = {}
        emails: dict[str, str] = {}
        normalised: dict[str, str] = {}
        for login, email in self.people.items():
            if not LOGIN_RE.fullmatch(login):
                raise ValueError(f"people login {login!r} is not a valid login")
            if login.casefold() in folded_people:
                raise ValueError(f"people login {login!r} is listed twice")
            folded_people[login.casefold()] = login
            if login.casefold() in bots:
                raise ValueError(f"people login {login!r} is a bot account")
            if email.count("@") != 1 or len(email) > MAX_EMAIL or not all(email.split("@")):
                raise ValueError(f"people {login} email {email!r} is not a valid email")
            low = email.lower()
            if low in emails:
                raise ValueError(f"email {low} belongs to two people")
            emails[low] = login
            normalised[login] = low
        self.people = normalised

        names = set(self.groups)
        repos: dict[str, str] = {}
        for name, group in self.groups.items():
            if not GROUP_RE.fullmatch(name):
                raise ValueError(f"group name {name!r} is not valid")
            if name.casefold() == "owners":
                raise ValueError(f"group name {name!r} is reserved")
            if f"{name}-approve" in names:
                raise ValueError(f"group {name}-approve collides with the approve team of group {name}")
            if group.repo is None:
                group.repo = f"kb-{name}"
                if not REPO_RE.fullmatch(group.repo):
                    raise ValueError(f"repo {group.repo!r} is not a valid repository name")
            if group.repo in repos:
                raise ValueError(f"repo {group.repo} is used by groups {repos[group.repo]} and {name}")
            repos[group.repo] = name
            _check_logins(group.members, f"group {name} members")
            _check_logins(group.approvers, f"group {name} approvers")
            if not group.approvers:
                raise ValueError(f"group {name} needs at least one approver")
            members = {m.casefold() for m in group.members}
            for login in group.members:
                if login.casefold() in bots:
                    raise ValueError(f"group {name} member {login} is a bot account")
                if login.casefold() not in folded_people:
                    raise ValueError(f"group {name} member {login} is not in people")
            for login in group.approvers:
                if login.casefold() not in members:
                    raise ValueError(f"group {name} approver {login} is not a member")

        table = _audience_table(self.people, self.groups)
        if len(table) > MAX_AUDIENCES:
            raise ValueError(
                "active groups are more than 200; "
                "PrivacyFence accepts at most 200 audiences"
            )
        if sum(len(v) for v in table.values()) > MAX_AUDIENCE_ADDRESSES:
            raise ValueError(
                f"active groups list more than {MAX_AUDIENCE_ADDRESSES} members in all; "
                f"PrivacyFence accepts at most {MAX_AUDIENCE_ADDRESSES} addresses"
            )
        answer = {"version": 1, "audiences": table}
        # PrivacyFence caps the text and the structured content separately, and the text is indented.
        size = max(len(json.dumps(answer)), len(pydantic_core.to_json(answer, indent=2).decode()))
        if size > MAX_AUDIENCE_ANSWER_CHARS:
            raise ValueError(
                f"the audience table is larger than {MAX_AUDIENCE_ANSWER_CHARS} characters; "
                "PrivacyFence refuses it"
            )

        if len(self.labels) > MAX_LABELS:
            raise ValueError(f"labels has more than {MAX_LABELS} entries")
        for label, description in self.labels.items():
            if not LABEL_RE.fullmatch(label):
                raise ValueError(f"label {label!r} is not a valid label name")
            if len(description) > MAX_LABEL_DESCRIPTION:
                raise ValueError(f"label {label} description is longer than {MAX_LABEL_DESCRIPTION}")
        seen_archived: set[str] = set()
        for name in self.archived_labels:
            if name not in self.labels:
                raise ValueError(f"archived_labels entry {name!r} is not in labels")
            if name in seen_archived:
                raise ValueError(f"archived_labels lists {name!r} twice")
            seen_archived.add(name)

        if self.tech_org:
            if not LOGIN_RE.fullmatch(self.tech_org):
                raise ValueError(f"tech_org {self.tech_org!r} is not a valid login")
            if self.tech_org.casefold() == self.org.casefold():
                raise ValueError("tech_org must differ from org")
        if self.tech_repos:
            if not self.tech_org:
                raise ValueError("tech_repos needs tech_org")
            if TECH_AUDIENCE not in self.groups:
                raise ValueError(f"tech_repos needs the group {TECH_AUDIENCE}")
            if self.groups[TECH_AUDIENCE].archived:
                raise ValueError(f"tech_repos needs the group {TECH_AUDIENCE} to be active")
            for repo_name, tech in self.tech_repos.items():
                if not REPO_RE.fullmatch(repo_name):
                    raise ValueError(f"tech repo {repo_name!r} is not a valid repository name")
                if repo_name in repos:
                    raise ValueError(f"tech repo {repo_name} is also the repo of group {repos[repo_name]}")
                seen_tech: set[str] = set()
                for label in tech.labels:
                    if label not in self.labels:
                        raise ValueError(f"tech repo {repo_name} label {label!r} is not in labels")
                    if label in seen_tech:
                        raise ValueError(f"tech repo {repo_name} lists label {label!r} twice")
                    seen_tech.add(label)
                if not tech.dirs:
                    raise ValueError(f"tech repo {repo_name} needs at least one dir")
                if len(tech.dirs) > MAX_TECH_DIRS:
                    raise ValueError(f"tech repo {repo_name} has more than {MAX_TECH_DIRS} dirs")
                seen_dirs: set[str] = set()
                for directory in tech.dirs:
                    if not DIR_RE.fullmatch(directory):
                        raise ValueError(f"tech repo {repo_name} dir {directory!r} is not a valid directory")
                    if directory in seen_dirs:
                        raise ValueError(f"tech repo {repo_name} lists dir {directory!r} twice")
                    seen_dirs.add(directory)
        return self

    def is_tech_repo(self, repo: str) -> bool:
        return repo in self.tech_repos

    def tech_labels(self, repo: str) -> list[str]:
        return list(self.tech_repos[repo].labels)

    def tech_dirs(self, repo: str) -> list[str]:
        return list(self.tech_repos[repo].dirs)

    @property
    def tech_repo_names(self) -> list[str]:
        return sorted(self.tech_repos)

    @property
    def active_groups(self) -> list[str]:
        return [name for name, group in self.groups.items() if not group.archived]

    def is_active(self, name: str) -> bool:
        return name in self.groups and not self.groups[name].archived

    @property
    def archived_label_set(self) -> frozenset[str]:
        return frozenset(self.archived_labels)

    def visible_labels(self, labels: Iterable[str]) -> list[str]:
        hidden = self.archived_label_set
        return [label for label in labels if label not in hidden]

    @property
    def bots(self) -> frozenset[str]:
        return frozenset({self.bot_login, self.indexer_login})

    def repo_for_group(self, name: str) -> str:
        repo = self.groups[name].repo
        assert repo is not None
        return repo

    def group_for_repo(self, repo: str) -> str | None:
        for name, group in self.groups.items():
            if group.repo == repo:
                return name
        return None

    def audiences_for_email(self, email: str | None) -> frozenset[str]:
        if not email:
            return frozenset()
        low = email.lower()
        logins = {login.casefold() for login, e in self.people.items() if e == low}
        if not logins:
            return frozenset()
        return frozenset(
            name
            for name, group in self.groups.items()
            if not group.archived and logins & {m.casefold() for m in group.members}
        )

    def audience_members(self) -> dict[str, list[str]]:
        return _audience_table(self.people, self.groups)


def _wrap(exc: ValidationError) -> ConfigError:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"])
    msg = first["msg"].removeprefix("Value error, ")
    if msg.startswith("config:"):
        return ConfigError(msg)
    if first["type"] == "value_error":
        return ConfigError(f"config: {msg}")
    return ConfigError(f"config: {msg}" + (f" ({where})" if where else ""))


def parse_config(data: dict[str, Any] | None) -> KhConfig:
    try:
        return KhConfig.model_validate(data or {})
    except ValidationError as exc:
        raise _wrap(exc) from exc


def load_config(path: str | Path | None = None) -> KhConfig:
    if path is None:
        return KhConfig()
    try:
        data = yaml.safe_load(Path(path).read_text())
    except OSError as exc:
        raise ConfigError(f"config: cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"config: invalid YAML in {path}") from exc
    if data is not None and not isinstance(data, dict):
        raise ConfigError("config: top level must be a mapping")
    return parse_config(data)
