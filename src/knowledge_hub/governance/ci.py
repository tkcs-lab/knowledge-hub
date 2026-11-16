"""``kh validate`` orchestration: reads from Forgejo and Grist, rules from ``rules.py`` (plan 3.5, 3.7)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from knowledge_hub.approvals import BOT_LOGINS, current_approvals, requester_from_statuses
from knowledge_hub.config import Config, owner_teams_for
from knowledge_hub.forgejo.client import ForgejoClient, ForgejoError
from knowledge_hub.governance.messages import (
    MAX_FILE_BYTES,
    MAX_FILES,
    Code,
    Finding,
    render_line,
    sort_findings,
)
from knowledge_hub.governance.owners import load_owners
from knowledge_hub.governance.owners_check import check_owners
from knowledge_hub.governance.rules import (
    FileInput,
    entity_kind_allowed,
    id_from_path,
    parse_layout,
    validate_files,
)
from knowledge_hub.grist.client import GristClient
from knowledge_hub.indexer.frontmatter import ParsedDoc, parse_document

CONTEXT = "kh/validate"
OWNERS_CONTEXT = "kh/domain-owners"
WIDEN_PREFIX = "widen-audience/"
WIDEN_LABEL_PROBLEM = "widen-audience needs a destination label widen-audience/<audience>"
_REASON = re.compile(r"(?mi)^Reason for widening:[ \t]*(\S.*)$")
READ_CONCURRENCY = 8


class CiError(RuntimeError):
    """A configuration problem that stops the run (exit 2)."""


@dataclass
class CheckResult:
    lines: list[str]
    ok: bool


def _is_404(exc: ForgejoError) -> bool:
    return "HTTP 404" in str(exc)


def _not_visible(exc: ForgejoError) -> bool:
    """The token cannot see the repository (403 or 404)."""
    text = str(exc)
    return text.endswith("HTTP 403") or text.endswith("HTTP 404")


async def build_id_index(
    read: ForgejoClient, config: Config, own_repo: str, head: str
) -> tuple[dict[str, list[str]], set[str]]:
    """(id to paths in the own repo's head tree, ids of the other T0 repos the token can read).

    A repository the token cannot see (403 or 404) is skipped: the check is best effort across
    audiences, and the hub scopes document ids per repo.
    """

    async def tree(repo: str) -> list[dict[str, Any]] | None:
        try:
            if repo == own_repo:
                sha: str | None = head
            else:
                sha = await read.head_sha_or_none(repo, "main")
            if sha is None:
                return None
            return await read.list_tree(repo, sha)
        except ForgejoError as exc:
            if repo == own_repo:
                raise
            if _not_visible(exc):
                return None
            raise CiError("another repository could not be read") from None

    others = [r for r, spec in config.repos.items() if spec.tier == "T0" and r != own_repo]
    results = await asyncio.gather(tree(own_repo), *(tree(r) for r in others))

    def ids(entries: list[dict[str, Any]] | None) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for e in entries or []:
            path = e.get("path")
            if e.get("type") == "blob" and isinstance(path, str):
                ident = id_from_path(path, config.domains)
                if ident is not None:
                    out.append((ident, path))
        return out

    in_repo: dict[str, list[str]] = {}
    for ident, path in ids(results[0]):
        in_repo.setdefault(ident, []).append(path)
    elsewhere: set[str] = set()
    for entries in results[1:]:
        elsewhere.update(ident for ident, _ in ids(entries))
    return in_repo, elsewhere


def _wanted(change: dict[str, Any]) -> bool:
    path = change["filename"]
    return (
        change.get("status") != "removed"
        and path.endswith(".md")
        and not any(p.startswith(".") for p in path.split("/"))
    )


async def _validate(
    read: ForgejoClient, grist: GristClient, config: Config, repo: str, base: str, head: str
) -> CheckResult:
    if repo not in config.repos:
        raise CiError(f"repository {repo} is not in the config")
    try:
        owners = load_owners(await read.get_raw(repo, "owners.yml", base))
    except ForgejoError as exc:
        if _is_404(exc):
            raise CiError("owners.yml is missing at the base commit") from None
        raise
    changes = await read.compare_files(repo, base, head)
    paths = sorted({c["filename"] for c in changes if _wanted(c)})
    if len(paths) > MAX_FILES:
        finding = Finding(paths[0], Code.TOO_MANY)
        return CheckResult([render_line(finding)], False)

    sem = asyncio.Semaphore(READ_CONCURRENCY)

    async def fetch(path: str) -> str:
        async with sem:
            return await read.get_raw(repo, path, head)

    texts = await asyncio.gather(*(fetch(p) for p in paths))
    findings: list[Finding] = []
    files: list[FileInput] = []
    for path, text in zip(paths, texts, strict=True):
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            findings.append(Finding(path, Code.TOO_LARGE))
        else:
            files.append(FileInput(path, text))

    in_repo, elsewhere = await build_id_index(read, config, repo, head)

    kinds: set[str] = set()
    for f in files:
        layout = parse_layout(f.path, config.domains)
        if layout is not None:
            for kind, folder in (("prd", layout.product), ("cmp", layout.component)):
                if folder is not None:
                    if kind not in config.entities:
                        raise CiError(f"config entities has no {kind} kind")
                    kinds.add(kind)
        doc = parse_document(f.text, f.path, config.domains)
        if isinstance(doc, ParsedDoc):
            audience = config.repos[repo].audience
            kinds.update(
                k
                for k in (ref.partition(":")[0] for ref in doc.entities)
                if entity_kind_allowed(config, k, audience)
            )
    ordered = sorted(kinds)
    tables = await asyncio.gather(*(grist.fetch_table(config.entities[k].table) for k in ordered))
    entity_keys: dict[str, set[str]] = {}
    for kind, records in zip(ordered, tables, strict=True):
        entity_keys[kind] = {r["key"] for r in records if isinstance(r.get("key"), str) and r["key"]}

    findings += validate_files(
        files,
        config=config,
        owners=owners,
        ids_in_repo=in_repo,
        ids_elsewhere=elsewhere,
        entity_keys=entity_keys,
        repo_audience=config.repos[repo].audience,
    )
    findings = sort_findings(findings)
    return CheckResult([render_line(f) for f in findings], not findings)


async def run_validate(
    read: ForgejoClient,
    status: ForgejoClient,
    grist: GristClient,
    config: Config,
    repo: str,
    base: str,
    head: str,
    *,
    post: bool = True,
) -> CheckResult:
    try:
        result = await _validate(read, grist, config, repo, base, head)
    except Exception:
        if post:
            try:
                await status.post_status(repo, head, CONTEXT, "error", "check could not run")
            except ForgejoError:
                pass
        raise
    if post:
        if result.ok:
            await status.post_status(repo, head, CONTEXT, "success", "all checks passed")
        else:
            await status.post_status(repo, head, CONTEXT, "failure", f"{len(result.lines)} problems")
    return result


def _widen(config: Config, labels: list[str]) -> tuple[tuple[str, list[str]] | None, str | None]:
    """(audience and its owner teams, label problem) from the PR's label names."""
    destinations = [n for n in labels if n.startswith(WIDEN_PREFIX)]
    if len(destinations) == 1 and destinations[0][len(WIDEN_PREFIX) :] in config.audiences:
        audience = destinations[0][len(WIDEN_PREFIX) :]
        return (audience, owner_teams_for(config, audience)), None
    if destinations or "widen-audience" in labels:
        return None, WIDEN_LABEL_PROBLEM
    return None, None


def _reason_ok(body: Any) -> bool:
    match = _REASON.search(body if isinstance(body, str) else "")
    return match is not None and not match.group(1).startswith("<")


async def _domain_owners(
    read: ForgejoClient, config: Config, repo: str, pr: dict[str, Any], number: int
) -> CheckResult:
    base_sha = pr["base"]["sha"]
    try:
        owners = load_owners(await read.get_raw(repo, "owners.yml", base_sha))
    except ForgejoError as exc:
        if _is_404(exc):
            raise CiError("owners.yml is missing at the base commit") from None
        raise
    changes = await read.compare_files(repo, pr.get("merge_base") or base_sha, pr["head"]["sha"])
    paths: set[str] = set()
    for change in changes:
        paths.add(change["filename"])
        if change.get("previous_filename"):
            paths.add(change["previous_filename"])
    reviews = await read.pull_reviews(repo, number)
    statuses = await read.commit_statuses(repo, pr["head"]["sha"])
    requesters, problem = requester_from_statuses(pr, statuses)
    counted = current_approvals(pr, reviews, BOT_LOGINS, exclude_logins=requesters)
    labels = [lab["name"] for lab in pr.get("labels") or [] if isinstance(lab.get("name"), str)]
    widen, label_problem = _widen(config, labels)

    teams: set[str] = set()
    for path in paths:
        rule = owners.rule_for(path)
        if rule is not None:
            teams.add(rule.team)
    if widen is not None:
        teams.update(widen[1])
    logins = sorted(
        {r["user"]["login"] for r in counted if isinstance(r.get("user"), dict) and r["user"].get("login")}
    )
    members: dict[str, set[str]] = {}
    for team in sorted(teams):
        tid = await read.team_id(team)
        if tid is None:
            raise CiError(f"team {team} does not exist in Forgejo")
        members[team] = {login for login in logins if await read.is_team_member(tid, login)}

    lines = check_owners(
        owners=owners,
        paths=sorted(paths),
        counted=counted,
        members=members,
        widen=widen,
        widen_reason_ok=_reason_ok(pr.get("body")),
        widen_label_problem=label_problem,
    )
    if problem is not None:
        lines = sorted([*lines, problem])
    return CheckResult(lines, not lines)


async def run_domain_owners(
    read: ForgejoClient,
    status: ForgejoClient,
    config: Config,
    repo: str,
    pr_number: int,
    *,
    post: bool = True,
) -> CheckResult:
    pr = await read.pull(repo, pr_number)
    if pr.get("state") != "open":
        return CheckResult(["pull request is not open"], True)
    head = pr["head"]["sha"]
    try:
        result = await _domain_owners(read, config, repo, pr, pr_number)
    except Exception:
        if post:
            try:
                await status.post_status(repo, head, OWNERS_CONTEXT, "error", "check could not run")
            except ForgejoError:
                pass
        raise
    if post:
        if result.ok:
            await status.post_status(repo, head, OWNERS_CONTEXT, "success", "all checks passed")
        else:
            await status.post_status(repo, head, OWNERS_CONTEXT, "failure", f"{len(result.lines)} problems")
    return result


__all__: Sequence[str] = ("CheckResult", "CiError", "build_id_index", "run_domain_owners", "run_validate")
