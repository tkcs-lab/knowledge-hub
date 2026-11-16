"""``kh validate`` orchestration: reads from Forgejo and Grist, rules from ``rules.py`` (plan 3.5, 3.7)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from knowledge_hub.config import Config
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
from knowledge_hub.governance.rules import FileInput, id_from_path, parse_layout, validate_files
from knowledge_hub.grist.client import GristClient
from knowledge_hub.indexer.frontmatter import ParsedDoc, parse_document

CONTEXT = "kh/validate"
READ_CONCURRENCY = 8


class CiError(RuntimeError):
    """A configuration problem that stops the run (exit 2)."""


@dataclass
class CheckResult:
    lines: list[str]
    ok: bool


def _is_404(exc: ForgejoError) -> bool:
    return "HTTP 404" in str(exc)


async def build_id_index(
    read: ForgejoClient, config: Config, own_repo: str, head: str
) -> tuple[dict[str, list[str]], set[str]]:
    """(id to paths in the own repo's head tree, ids of every other T0 repo's main tree)."""

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
            if repo != own_repo and _is_404(exc):
                return None
            raise

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
            kinds.update(k for k in (ref.partition(":")[0] for ref in doc.entities) if k in config.entities)
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


__all__: Sequence[str] = ("CheckResult", "CiError", "build_id_index", "run_validate")
