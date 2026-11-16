"""Shared self-merge cases (plan 3.8): load ``tests/data/self_merge/cases.yaml`` and build the worlds.

``build_trust_world`` builds the trust walk's world. The ``kh domain-owners`` world comes with p6.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from knowledge_hub.config import Config, parse_config
from knowledge_hub.governance.owners import build_owners, dump_owners
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake, approval, pr

REPO = "canonical-all"
CASES_FILE = Path(__file__).parent.parent / "data" / "self_merge" / "cases.yaml"
MERGED_AT = "2026-11-03T10:00:00Z"
COMMIT_DATE = "2026-11-02T09:00:00Z"


def load_cases() -> list[dict[str, Any]]:
    return yaml.safe_load(CASES_FILE.read_text())["cases"]


def case_ids() -> list[str]:
    return [c["name"] for c in load_cases()]


def default_owners() -> str:
    return dump_owners(
        build_owners(
            "all",
            portfolio_team="kh-portfolio",
            product_team="kh-product",
            technical_team="kh-technical",
            owner_team="kh-admins",
            admin_team="kh-admins",
        )
    )


def body_for(path: str, mark: str = "") -> str:
    """A valid document for a ``.md`` path, else plain text."""
    if not path.endswith(".md"):
        return f"{path}{mark}\n"
    domain = path.split("/")[0] if path.split("/")[0] in ("portfolio", "product", "technical") else "product"
    doc_id = path.removesuffix(".md").replace("/", "-")
    head = f"---\nid: {doc_id}\ntitle: T\ndomain: {domain}\nreview_by: 2027-01-01\n---\n"
    return f"{head}Body of {doc_id}.{mark}\n"


def _pr_files(case: dict[str, Any]) -> dict[str, str | None]:
    files: dict[str, str | None] = {p: body_for(p, " edited") for p in case.get("files", [])}
    files.update({p: None for p in case.get("deleted", [])})
    if case.get("owners_change"):
        files["owners.yml"] = case["owners_change"]
    return files


def build_trust_world(case: dict[str, Any]) -> tuple[dict[str, FakeRepo], Config]:
    root: dict[str, str | None] = {p: body_for(p) for p in case.get("base_files", [])}
    if case.get("owners_root") != "missing":
        root["owners.yml"] = default_owners()
    commits = [FakeCommit("a0", [], root)]
    pulls: list[dict[str, Any]] = []
    reviews: dict[int, list[dict[str, Any]]] = {}
    parent = "a0"
    for i, step in enumerate(case.get("history", []), start=1):
        sha, head = f"p{i}", f"hp{i}"
        files: dict[str, str | None] = {}
        if step.get("owners_change"):
            files["owners.yml"] = step["owners_change"]
        commits.append(FakeCommit(sha, [parent, head], files, COMMIT_DATE))
        pulls.append(
            pr(i, sha, head, step["author"], merged_by=step["merger"], merged_at=MERGED_AT)
        )
        reviews[i] = [approval(head, "bob", MERGED_AT)] if step.get("approved") else []
        parent = sha
    number = len(case.get("history", [])) + 1
    head = "h-case"
    second = "other-head" if case.get("head_mismatch") else head
    commits.append(FakeCommit("m1", [parent, second], _pr_files(case), COMMIT_DATE))
    if not case.get("no_pr"):
        p = pr(
            number,
            "m1",
            head,
            case["author"],
            merged_by=case["merger"],
            merged_at=MERGED_AT,
            author_id=case.get("author_id"),
            merged_by_id=case.get("merger_id"),
        )
        if case.get("labels"):
            p["labels"] = [{"name": n} for n in case["labels"]]
        pulls.append(p)
        reviews[number] = [approval(head, login, MERGED_AT) for login in case.get("approvals", [])]
    repo = FakeRepo(commits, pulls, reviews)
    config = parse_config(
        {
            "repos": {REPO: {"tier": "T0", "audience": "all", "trust_anchor": "a0"}},
            "self_merge_teams": case.get("self_merge_teams", []),
            "bot_logins": ["kh-hub-bot", *case.get("bots", [])],
        }
    )
    return {REPO: repo}, config


def make_fake(case: dict[str, Any], repos: dict[str, FakeRepo], teams: dict | None = None) -> ForgejoFake:
    given = case.get("teams", {}) if teams is None else teams
    return ForgejoFake(repos, teams={t: set(m) for t, m in given.items()})
