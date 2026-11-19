"""Pure rules of the T0a technical documents (plan section 3.4). No I/O."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from knowledge_hub.config import TECH_AUDIENCE, KhConfig
from knowledge_hub.indexer.frontmatter import strip_frontmatter
from knowledge_hub.indexer.sync_plan import MAX_FILE_BYTES, DocUpsert, SyncPlan

TECH_TIER = "T0a"
MAX_KEY = 100
MAX_TITLE = 200
_HEADING_RE = re.compile(r"^# +(\S.*?)[ #]*$")
_FENCES = ("```", "~~~")


def is_tech_included(entry: Mapping[str, Any], dirs: Collection[str] = ("docs",)) -> bool:
    path = entry["path"]
    if entry.get("type", "blob") != "blob" or int(entry.get("size", 0)) > MAX_FILE_BYTES:
        return False
    if any(seg.startswith(".") for seg in path.split("/")):
        return False
    if path == "CLAUDE.md":
        return True
    return path.endswith(".md") and any(path.startswith(f"{d}/") for d in dirs)


def tech_doc_key(path: str) -> str:
    if len(path) <= MAX_KEY:
        return path
    return path[: MAX_KEY - 9] + "~" + hashlib.sha256(path.encode()).hexdigest()[:8]


def tech_title(repo: str, path: str, body: str) -> str:
    if path == "CLAUDE.md":
        return f"{repo}: CLAUDE.md"[:MAX_TITLE]
    in_fence = False
    for line in body.splitlines():
        if line.startswith(_FENCES):
            in_fence = not in_fence
        elif not in_fence and (m := _HEADING_RE.match(line)):
            return m.group(1)[:MAX_TITLE]
    name = path.rsplit("/", 1)[-1]
    return name.removesuffix(".md")[:MAX_TITLE]


def tech_review_by(today: dt.date) -> dt.date:
    return dt.date(today.year + 100, today.month, min(today.day, 28))


def changed_tech_paths(
    tree: Iterable[Mapping[str, Any]],
    stored: Iterable[tuple[str, str, str]],
    dirs: Collection[str] = ("docs",),
) -> list[str]:
    have = {path: blob for path, blob, _ in stored}
    return [e["path"] for e in tree if is_tech_included(e, dirs) and have.get(e["path"]) != e["sha"]]


def build_tech_plan(
    config: KhConfig,
    repo: str,
    head_sha: str,
    tree: list[dict[str, Any]],
    stored: list[tuple[str, str, str]],
    texts: Mapping[str, str | None],
    known_labels: Collection[str],
    today: dt.date,
) -> SyncPlan:
    known = set(known_labels)
    configured = config.tech_labels(repo)
    labels = [x for x in configured if x in known]
    plan = SyncPlan(repo=repo, head_sha=head_sha, relabel=labels)
    missing = [x for x in configured if x not in known]
    alerts = [("", f"label {x} is not in kh.labels; run kh setup apply") for x in missing]
    path_alerts: dict[str, str] = {}
    review_by = tech_review_by(today)
    have = {path: blob for path, blob, _ in stored}
    present: set[str] = set()
    unreadable: set[str] = set()
    dirs = config.tech_dirs(repo)
    for entry in tree:
        if not is_tech_included(entry, dirs):
            continue
        path, sha = entry["path"], entry["sha"]
        if have.get(path) == sha:
            present.add(path)
            continue
        if path not in texts:
            raise ValueError(f"no text for changed file {path}")
        text = texts[path]
        if text is None:
            unreadable.add(path)
            path_alerts[path] = "file skipped: not UTF-8"
            continue
        present.add(path)
        body = strip_frontmatter(text).replace("\x00", "")
        plan.upserts.append(
            DocUpsert(
                repo, path, tech_doc_key(path), TECH_AUDIENCE, tech_title(repo, path, body), body,
                list(labels), review_by, head_sha, sha, supersedes=[], tier=TECH_TIER,
            )
        )  # fmt: skip
    plan.deletes = [path for path, _, _ in stored if path not in present or path in unreadable]
    plan.alerts = alerts + sorted(path_alerts.items())
    return plan
