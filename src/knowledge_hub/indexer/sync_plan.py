"""Pure planning of one repo sync: which files become documents, which rows go (plan section 3.6).

No I/O. The caller lists the tree, reads the stored rows and fetches the text of every changed file
(``changed_paths``); ``build_sync_plan`` decides what to upsert, what to delete and what to alert.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from knowledge_hub.config import KhConfig
from knowledge_hub.indexer.frontmatter import ParsedDoc, parse_document

MAX_FILE_BYTES = 1048576
REJECTED = "file rejected: "


@dataclass(frozen=True)
class DocUpsert:
    repo: str
    path: str
    doc_key: str
    audience: str
    title: str
    body: str
    labels: list[str]
    review_by: dt.date
    commit_sha: str
    blob_sha: str
    supersedes: list[str] = field(default_factory=list)


@dataclass
class SyncPlan:
    repo: str
    head_sha: str
    upserts: list[DocUpsert] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)  # stored paths to delete
    alerts: list[tuple[str, str]] = field(default_factory=list)  # (path, message)


def is_included(entry: Mapping[str, Any]) -> bool:
    """Whether a tree entry is a candidate document (rules of plan section 3.6)."""
    path = entry["path"]
    if entry.get("type", "blob") != "blob" or not path.endswith(".md") or path == "README.md":
        return False
    if any(seg.startswith(".") for seg in path.split("/")):
        return False
    return int(entry.get("size", 0)) <= MAX_FILE_BYTES


def changed_paths(tree: Iterable[Mapping[str, Any]], stored: Iterable[tuple[str, str, str]]) -> list[str]:
    """Included paths that are not stored with the same blob sha: their text is needed."""
    have = {path: blob for path, blob, _ in stored}
    return [e["path"] for e in tree if is_included(e) and have.get(e["path"]) != e["sha"]]


def build_sync_plan(
    config: KhConfig,
    repo: str,
    head_sha: str,
    tree: list[dict[str, Any]],
    stored: list[tuple[str, str, str]],
    texts: Mapping[str, str],
    known_labels: Collection[str],
) -> SyncPlan:
    """``tree``: entries ``{path, sha}`` (and ``type``, ``size``); ``stored``: ``(path, blob_sha, doc_key)``;
    ``texts``: the text of every path ``changed_paths`` names."""
    audience = config.group_for_repo(repo)
    if audience is None:
        raise ValueError(f"repo {repo} is in no group")
    labels_ok = set(known_labels)
    stored_by_path = {path: (blob, key) for path, blob, key in stored}
    plan = SyncPlan(repo=repo, head_sha=head_sha)

    # path -> (doc_key, DocUpsert or None for an unchanged stored file)
    holders: dict[str, tuple[str, DocUpsert | None]] = {}
    rejected: dict[str, str] = {}
    for entry in tree:
        if not is_included(entry):
            continue
        path, sha = entry["path"], entry["sha"]
        known = stored_by_path.get(path)
        if known is not None and known[0] == sha:
            holders[path] = (known[1], None)
            continue
        if path not in texts:
            raise ValueError(f"no text for changed file {path}")
        parsed = parse_document(texts[path], path)
        if not isinstance(parsed, ParsedDoc):
            rejected[path] = f"{REJECTED}{parsed.reason}"
            continue
        unknown = next((x for x in parsed.labels if x not in labels_ok), None)
        if unknown is not None:
            rejected[path] = f"{REJECTED}unknown label {unknown}"
            continue
        holders[path] = (
            parsed.id,
            DocUpsert(
                repo, path, parsed.id, audience, parsed.title, parsed.body, parsed.labels,
                parsed.review_by, head_sha, sha, supersedes=parsed.supersedes,
            ),
        )  # fmt: skip

    by_id: dict[str, list[str]] = defaultdict(list)
    for path, (key, _) in holders.items():
        by_id[key].append(path)
    for key, paths in by_id.items():
        if len(paths) > 1:
            for path in paths:
                rejected[path] = f"{REJECTED}duplicate id {key}"
                del holders[path]

    plan.upserts = [up for _, up in holders.values() if up is not None]
    # A stored row goes when its path left the tree or was rejected, or the file there now has another id.
    plan.deletes = [
        path for path, (_, key) in stored_by_path.items() if path not in holders or holders[path][0] != key
    ]
    plan.alerts = list(rejected.items())
    return plan
