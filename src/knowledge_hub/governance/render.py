"""Render the governance files of a canonical repository."""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

from knowledge_hub.governance.owners import build_owners, dump_owners, render_codeowners
from knowledge_hub.governance.schema import dump_schema

_AUDIENCE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_REVIEW_LINES = (
    "  # Forgejo v15 does not document this trigger; see docs/governance.md.\n"
    "  pull_request_review:\n"
    "    types: [submitted, dismissed]\n"
)
RUNNER_LABEL = "kh-validate:host"
_RUNNER = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}(:host)?")
_MARKER_LINE = "{{REVIEW_TRIGGER}}\n"


def _template(name: str) -> str:
    return (files("knowledge_hub.governance") / "templates" / name).read_text(encoding="utf-8")


def render_governance(
    out: Path,
    *,
    repo: str,
    audience: str,
    org: str = "kh",
    portfolio_team: str = "owners-portfolio",
    product_team: str = "owners-product",
    technical_team: str = "owners-technical",
    owner_team: str | None = None,
    admin_team: str = "kh-admins",
    review_trigger: bool = False,
    runner_label: str = RUNNER_LABEL,
) -> list[Path]:
    if not _AUDIENCE.match(audience):
        raise ValueError("render: audience must match ^[a-z0-9][a-z0-9_-]{0,62}$")
    if not _RUNNER.fullmatch(runner_label):
        raise ValueError("render: runner label must match ^[a-z0-9][a-z0-9._-]{0,62}(:host)?$")
    owners = build_owners(
        audience,
        portfolio_team=portfolio_team,
        product_team=product_team,
        technical_team=technical_team,
        owner_team=owner_team or f"owners-{audience}",
        admin_team=admin_team,
    )
    workflow = _template("kh.yml.tmpl").replace(_MARKER_LINE, _REVIEW_LINES if review_trigger else "")
    workflow = workflow.replace("{{RUNS_ON}}", runner_label.partition(":")[0])
    contents = {
        Path("schema/frontmatter.schema.json"): dump_schema(),
        Path("owners.yml"): dump_owners(owners),
        Path(".forgejo/CODEOWNERS"): render_codeowners(owners, org),
        Path(".forgejo/pull_request_template.md"): _template("pull_request_template.md"),
        Path(".forgejo/workflows/kh.yml"): workflow,
    }
    written = []
    for rel, text in contents.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return sorted(written)
