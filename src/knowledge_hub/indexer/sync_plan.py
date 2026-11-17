"""The pure document plan of one repo sync (plan section 3.6).

``build_sync_plan`` reads Forgejo and returns a ``SyncPlan`` of plain lists; it makes no database call.
The next phase writes the plan in one transaction.

``repo_state`` is ``None`` or a mapping with ``last_indexed_sha``. ``existing_docs`` maps ``doc_key`` to
a mapping with ``path``, ``content_sha``, ``commit_sha``, ``status``, ``approved_by``, ``approved_at``
and ``merge_pr`` (any may be missing).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from knowledge_hub.approvals import current_approvals, requester_from_statuses
from knowledge_hub.config import Config
from knowledge_hub.forgejo.client import ForgejoError
from knowledge_hub.indexer.frontmatter import ParsedDoc, Rejected, parse_document
from knowledge_hub.indexer.trust import (
    TRUSTED,
    MergeInput,
    Verdict,
    evaluate_chain,
    parse_forgejo_ts,
)
from knowledge_hub.merge_io import TeamDirectory, owners_at, tree_paths_changed
from knowledge_hub.merge_rule import is_bot, same_person, teams_to_check

BRANCH = "main"
REWRITTEN = "main of {repo} was rewritten; run kh rebuild after review"
NOT_APPROVED = "file {path} on {repo}/main has no approved merge PR"
BAD_REFERENCE = "supersedes/superseded_by must name a document in the same repo"
DUPLICATE = "duplicate id {id} in {repo}: {paths}"


@dataclass
class IndexAlert:
    repo: str
    path: str | None
    message: str


@dataclass
class DocUpsert:
    repo: str
    doc_key: str
    path: str
    tier: str
    domain: str
    title: str
    body: str
    frontmatter: dict[str, Any]
    status: str  # current | superseded | disputed
    review_by: str
    owner: str | None
    entities: list[str]
    supersedes: list[str]
    superseded_by: str | None  # doc_key of the current document that supersedes this one
    commit_sha: str
    content_sha: str
    approved_by: list[str]
    approved_at: str | None
    merge_pr: int | None
    pr_url: str | None
    stamp: str | None
    required_audiences: list[str]
    allowed_emails: list[str] | None
    agent_written: bool = False
    changed: bool = True  # differs from the existing row


@dataclass
class DocVersion:
    repo: str
    doc_key: str
    commit_sha: str
    path: str
    tier: str
    status: str
    approved_by: list[str]
    approved_at: str | None
    stamp: str | None = None


@dataclass
class Rename:
    doc_key: str
    old_path: str
    new_path: str


@dataclass
class SyncPlan:
    repo: str
    refused: bool = False
    head_sha: str | None = None
    upserts: list[DocUpsert] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)  # doc_keys of rejected files
    deletions: list[str] = field(default_factory=list)  # doc_keys absent from the tree
    renames: list[Rename] = field(default_factory=list)
    versions: list[DocVersion] = field(default_factory=list)
    alerts: list[IndexAlert] = field(default_factory=list)
    # New repo_state values; ``None`` when the sync was refused (state unchanged).
    repo_state: dict[str, Any] | None = None
    # Merge verdicts judged in this run, to store (plan section 3.5).
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    rejudge: bool = False


@dataclass
class _File:
    path: str
    text: str
    doc: ParsedDoc
    verdict: Verdict | None
    commit_sha: str


async def _gather_trust(
    forgejo: Any,
    repo: str,
    chain: list[dict[str, Any]],
    anchor: str | None,
    config: Config,
    stored: Mapping[str, Verdict],
):
    """Collect PRs, reviews and changed files, then run the trust walk."""
    oldest_first = list(reversed(chain))
    start = 0
    if anchor is not None:
        idx = next((i for i, c in enumerate(oldest_first) if c["sha"] == anchor), None)
        if idx is not None:
            start = idx + 1
    prs: list[dict[str, Any]] = []
    reviews: dict[int, list[dict[str, Any]]] = {}
    changed: dict[str, list[str]] = {}
    inputs: dict[str, MergeInput] = {}
    requesters: dict[int, tuple[tuple[str, ...], str | None]] = {}
    directory = TeamDirectory(forgejo)
    bots = config.bots
    for c in oldest_first[start:]:
        parents = c.get("parents") or []
        if parents:
            changed[c["sha"]] = await forgejo.changed_files(repo, parents[0]["sha"], c["sha"])
        if len(parents) < 2 or c["sha"] in stored:
            continue
        pull = await forgejo.pull_for_commit(repo, c["sha"])
        if pull is None:
            continue
        prs.append(pull)
        pr_reviews = await forgejo.pull_reviews(repo, pull["number"])
        reviews[pull["number"]] = pr_reviews
        record = requester_from_statuses(pull, await forgejo.commit_statuses(repo, pull["head"]["sha"]))
        if record != ((), None):
            requesters[pull["number"]] = record
        logins, problem = record
        if (
            problem is not None
            or not config.self_merge_teams
            or current_approvals(pull, pr_reviews, bots, exclude_logins=logins)
        ):
            continue
        user, merger = pull.get("user") or {}, pull.get("merged_by") or {}
        author = user.get("login")
        if not same_person(author, merger.get("login"), user.get("id"), merger.get("id")):
            continue
        if is_bot(author, bots):
            continue
        owners = await owners_at(forgejo, repo, parents[0]["sha"])
        paths = await tree_paths_changed(forgejo, repo, parents[0]["sha"], c["sha"])
        teams = await directory.teams_of(
            author, teams_to_check(owners, paths, config.self_merge_teams)
        )
        inputs[c["sha"]] = MergeInput(owners, paths, teams)
    result = evaluate_chain(
        repo,
        chain,
        prs,
        reviews,
        changed,
        bots,
        anchor,
        stored=stored,
        inputs=inputs,
        self_merge_teams=config.self_merge_teams,
        requesters=requesters,
    )
    return result, changed


def _last_commit_for(
    path: str, chain: list[dict[str, Any]], changed: dict[str, list[str]], default: str
) -> str:
    for c in chain:  # newest first
        if path in changed.get(c["sha"], ()):
            return c["sha"]
    return default


def _is_agent_written(tier: str, path: str, doc: ParsedDoc) -> bool:
    """A T1 file under ``agent/`` or of type ``agent-note``; removing the type key does not unmark it."""
    return tier == "T1" and (path.startswith("agent/") or doc.type == "agent-note")


def _approved(f: _File) -> bool:
    return f.verdict is not None and f.verdict.status == TRUSTED


def _doc_changed(row: Mapping[str, Any] | None, up: DocUpsert) -> bool:
    if row is None:
        return True
    pairs = (
        ("path", up.path),
        ("content_sha", up.content_sha),
        ("commit_sha", up.commit_sha),
        ("status", up.status),
        ("approved_by", up.approved_by),
        ("approved_at", up.approved_at),
        ("merge_pr", up.merge_pr),
        ("stamp", up.stamp),
        ("agent_written", up.agent_written),
    )
    for key, value in pairs:
        old = row.get(key)
        if key == "approved_by":
            old = list(old or [])
        if key == "agent_written":
            old = bool(old)
        if key == "approved_at":
            old, value = parse_forgejo_ts(old), parse_forgejo_ts(value)
            old = old and old.replace(microsecond=0)
            value = value and value.replace(microsecond=0)
        if old != value:
            return True
    return False


def _statuses(files: dict[str, _File], repo: str, alerts: list[IndexAlert]):
    """Status, superseding key and alerts per doc_key (same-repo rules of 3.6)."""
    status: dict[str, str] = {}
    reason_disputed: set[str] = set()
    for key, f in files.items():
        if f.verdict is not None and f.verdict.status != TRUSTED:
            status[key] = "disputed"
            reason_disputed.add(key)
            if f.verdict.reason != "non-merge commit":
                alerts.append(IndexAlert(repo, f.path, NOT_APPROVED.format(path=f.path, repo=repo)))
        else:
            status[key] = f.doc.status
    for key, f in files.items():
        refs = [*f.doc.supersedes, *f.doc.superseded_by]
        if any(r not in files or r == key for r in refs):
            if status[key] != "disputed":
                alerts.append(IndexAlert(repo, f.path, BAD_REFERENCE))
            status[key] = "disputed"
    superseded_by: dict[str, str] = {}
    for key, f in sorted(files.items()):
        if status[key] != "current":
            continue
        for target in f.doc.supersedes:
            if status[target] != "disputed":
                superseded_by.setdefault(target, key)
    for key, f in files.items():
        for by in f.doc.superseded_by:
            if status[key] != "disputed" and status.get(by) == "current":
                superseded_by.setdefault(key, by)
    for target in superseded_by:
        if status[target] != "disputed":
            status[target] = "superseded"
    return status, superseded_by


async def build_sync_plan(
    forgejo: Any,
    config: Config,
    repo: str,
    repo_state: Mapping[str, Any] | None,
    existing_docs: Mapping[str, Mapping[str, Any]],
    stored_verdicts: Mapping[str, Verdict] | None = None,
    rejudge: bool = False,
) -> SyncPlan:
    if repo not in config.repos:
        raise ValueError(f"repo {repo} is not listed in config")
    rc = config.repos[repo]
    plan = SyncPlan(repo=repo, rejudge=rejudge)

    head = await forgejo.head_sha(repo, BRANCH)
    chain = await forgejo.commits_first_parent(repo, BRANCH)
    if not chain or chain[0]["sha"] != head:
        raise ForgejoError(f"main of {repo} moved while planning; retry")
    plan.head_sha = head
    last = (repo_state or {}).get("last_indexed_sha")
    if last is not None and last not in {c["sha"] for c in chain}:
        plan.refused = True
        plan.alerts.append(IndexAlert(repo, None, REWRITTEN.format(repo=repo)))
        return plan

    canonical = rc.tier == "T0"
    anchor = rc.trust_anchor
    trusted, trust_alerts, trust_result = True, [], None
    if canonical:
        stored = {} if rejudge else dict(stored_verdicts or {})
        trust_result, changed = await _gather_trust(forgejo, repo, chain, anchor, config, stored)
        plan.verdicts = dict(trust_result.judged)
        trusted, trust_alerts = trust_result.trusted, list(trust_result.alerts)
    else:
        changed = {}
        oldest_first = list(reversed(chain))
        stop = next((i for i, c in enumerate(oldest_first) if c["sha"] == last), -1)
        for c in oldest_first[stop + 1 :]:
            parents = c.get("parents") or []
            if parents:
                changed[c["sha"]] = await forgejo.changed_files(repo, parents[0]["sha"], c["sha"])
    for message in trust_alerts:
        if not message.startswith("file "):
            plan.alerts.append(IndexAlert(repo, None, message))

    tree = await forgejo.list_tree(repo, head)
    existing_by_path = {row.get("path"): key for key, row in existing_docs.items()}
    anchor_sha = anchor or chain[-1]["sha"]
    candidates: list[tuple[str, str, ParsedDoc]] = []
    rejected_paths: list[str] = []
    for entry in sorted(tree, key=lambda e: e["path"]):
        path = entry["path"]
        if (
            entry.get("type") != "blob"
            or not path.endswith(".md")
            or any(seg.startswith(".") for seg in path.split("/"))
        ):
            continue
        text = await forgejo.get_raw(repo, path, head)
        parsed = parse_document(text, path, config.domains)
        if isinstance(parsed, Rejected):
            rejected_paths.append(path)
            plan.alerts.append(IndexAlert(repo, path, parsed.reason))
        else:
            candidates.append((path, text, parsed))

    def make_file(path: str, text: str, doc: ParsedDoc) -> _File:
        default = existing_docs.get(doc.id, {}).get("commit_sha") or anchor_sha
        commit = _last_commit_for(path, chain, changed, default)
        verdict = trust_result.verdict_for(path) if trust_result is not None else None
        return _File(path, text, doc, verdict, commit)

    by_id: dict[str, list[_File]] = {}
    for path, text, doc in candidates:
        by_id.setdefault(doc.id, []).append(make_file(path, text, doc))
    files: dict[str, _File] = {}
    for doc_id, group in by_id.items():
        if len(group) > 1:
            paths = ", ".join(sorted(f.path for f in group))
            message = DUPLICATE.format(id=doc_id, repo=repo, paths=paths)
            rejected_paths.extend(f.path for f in group)
            for f in sorted(group, key=lambda f: f.path):
                plan.alerts.append(IndexAlert(repo, f.path, message))
            # The one file last changed by an approved merge could stay, but only when no other
            # file with the id was also approved; with two or more approved all are rejected.
            approved = [f for f in group if _approved(f)]
            human = [f for f in group if not _is_agent_written(rc.tier, f.path, f.doc)]
            keep = None
            if len(approved) == 1:
                keep = approved[0]
            elif rc.tier == "T1" and len(human) == 1:
                # A squatted id cannot remove a human note: keep it, reject only the agent notes.
                keep = human[0]
            if keep is not None:
                rejected_paths.remove(keep.path)
                files[doc_id] = keep
        else:
            files[doc_id] = group[0]

    status, superseded_by = _statuses(files, repo, plan.alerts)

    for key, f in sorted(files.items()):
        v = f.verdict
        trusted_file = v is not None and v.status == TRUSTED and status[key] != "disputed"
        d = f.doc
        up = DocUpsert(
            repo=repo,
            doc_key=key,
            path=f.path,
            tier=rc.tier,
            domain=d.domain,
            title=d.title,
            body=d.body,
            frontmatter={
                "id": d.id,
                "title": d.title,
                "domain": d.domain,
                "review_by": d.review_by.isoformat(),
                "owner": d.owner,
                "type": d.type,
                "entities": d.entities,
                "supersedes": d.supersedes,
                "superseded_by": d.superseded_by,
                "status": d.status,
            },
            status=status[key],
            review_by=d.review_by.isoformat(),
            owner=d.owner,
            entities=list(d.entities),
            supersedes=list(d.supersedes),
            superseded_by=superseded_by.get(key),
            commit_sha=f.commit_sha,
            content_sha=hashlib.sha256(f.text.encode("utf-8")).hexdigest(),
            approved_by=list(v.approved_by) if trusted_file and v is not None else [],
            approved_at=v.approved_at if trusted_file and v is not None else None,
            merge_pr=v.merge_pr if v is not None else None,
            pr_url=v.pr_url if v is not None else None,
            stamp=v.stamp if trusted_file and v is not None else None,
            required_audiences=[rc.audience],
            allowed_emails=None,
            agent_written=_is_agent_written(rc.tier, f.path, d),
        )
        row = existing_docs.get(key)
        up.changed = _doc_changed(row, up)
        plan.upserts.append(up)
        if row is not None and row.get("path") != f.path:
            plan.renames.append(Rename(key, row["path"], f.path))
        if row is None or row.get("commit_sha") != up.commit_sha:
            plan.versions.append(
                DocVersion(
                    repo,
                    key,
                    up.commit_sha,
                    up.path,
                    up.tier,
                    up.status,
                    up.approved_by,
                    up.approved_at,
                    up.stamp,
                )
            )

    for path in rejected_paths:
        key = existing_by_path.get(path)
        if key is not None and key not in files and key not in plan.removals:
            plan.removals.append(key)
    plan.deletions = sorted(k for k in existing_docs if k not in files and k not in plan.removals)

    reason = next((a for a in trust_alerts if not a.startswith("file ")), None)
    plan.repo_state = {
        "repo": repo,
        "trusted": trusted,
        "untrusted_reason": None if trusted else reason,
        "last_indexed_sha": head,
        "trust_anchor_sha": anchor,
    }
    return plan
