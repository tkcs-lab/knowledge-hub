"""``owners.yml`` model, glob matching and ``CODEOWNERS`` rendering. Errors start with ``owners.yml:``."""

from __future__ import annotations

import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

GOVERNANCE_PATHS = (".forgejo/**", "schema/**", "owners.yml", "CODEOWNERS", "glossary.md")
DOMAINS = ("portfolio", "product", "technical")


class OwnersError(ValueError):
    pass


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team: str
    paths: list[str]


class OwnersFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    audience: str
    domains: dict[str, str]
    rules: list[Rule]

    def rule_for(self, path: str) -> Rule | None:
        for rule in self.rules:
            if any(glob_to_regex(p).fullmatch(path) for p in rule.paths):
                return rule
        return None


def load_owners(text: str) -> OwnersFile:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise OwnersError("owners.yml: invalid YAML") from exc
    try:
        return OwnersFile.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise OwnersError(f"owners.yml: {first['msg']}" + (f" ({where})" if where else "")) from exc


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out), re.DOTALL)


def build_owners(
    audience: str,
    *,
    portfolio_team: str,
    product_team: str,
    technical_team: str,
    owner_team: str,
    admin_team: str,
) -> OwnersFile:
    governance = Rule(team=admin_team, paths=list(GOVERNANCE_PATHS))
    if audience == "all":
        return OwnersFile(
            version=1,
            audience=audience,
            domains={"portfolio": portfolio_team, "product": product_team, "technical": technical_team},
            rules=[
                governance,
                Rule(team=portfolio_team, paths=["portfolio/**"]),
                Rule(team=product_team, paths=["product/**"]),
                Rule(team=technical_team, paths=["technical/**"]),
                Rule(team=admin_team, paths=["**"]),
            ],
        )
    return OwnersFile(
        version=1,
        audience=audience,
        domains={d: owner_team for d in DOMAINS},
        rules=[governance, Rule(team=owner_team, paths=["**"])],
    )


def dump_owners(o: OwnersFile) -> str:
    data = {
        "version": o.version,
        "audience": o.audience,
        "domains": dict(o.domains),
        "rules": [{"team": r.team, "paths": list(r.paths)} for r in o.rules],
    }
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def _codeowners_pattern(pattern: str) -> str:
    if pattern == "**":
        return "*"
    if pattern.endswith("/**"):
        return "/" + pattern[:-2]
    return "/" + pattern


def render_codeowners(o: OwnersFile, org: str) -> str:
    lines = ["# Generated from owners.yml; the CI check is authoritative."]
    for rule in reversed(o.rules):
        for path in rule.paths:
            lines.append(f"{_codeowners_pattern(path)} @{org}/{rule.team}")
    return "\n".join(lines) + "\n"
