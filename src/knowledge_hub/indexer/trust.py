"""Trust walk over the first-parent chain of ``main`` (plan section 3.6).

Pure: no I/O. The caller fetches Forgejo records and passes them in the shapes of the Forgejo API.

Inputs
- ``commits``: the first-parent chain of ``main`` as commit objects (``sha``, ``parents[].sha``,
  ``commit.committer.date``), newest first (HEAD first), ending at the root.
- ``prs``: pull request objects (``number``, ``html_url``, ``merged``, ``merge_commit_sha``,
  ``user.login``, ``base.ref``, ``head.sha``).
- ``reviews``: reviews per pull request number (``official``, ``dismissed``, ``state``,
  ``commit_id``, ``submitted_at``, ``user.login``).
- ``changed_files``: file paths changed by each commit sha (see ``files_from_compare``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from knowledge_hub.approvals import current_approvals

TRUSTED = "trusted"
DISPUTED = "disputed"
TRUST_ANCHOR = "trust-anchor"
NON_MERGE = "non-merge commit"
BAD_TIMESTAMP = "unparsable timestamp"
_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


def parse_forgejo_ts(value: Any) -> datetime | None:
    """A Forgejo timestamp as an aware UTC datetime, or ``None`` if it cannot be parsed.

    Accepts ``Z``, numeric offsets (``+01:00``) and fractional seconds. A value without an offset
    is refused: its instant is unknown.
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


def format_ts(dt: datetime) -> str:
    """Whole-second UTC with ``Z``: the stored form (same as ``sync._fmt_ts``)."""
    return dt.astimezone(UTC).strftime(_ISO_Z)


@dataclass(frozen=True)
class Verdict:
    status: str  # "trusted" | "disputed"
    approved_by: tuple[str, ...] = ()
    approved_at: str | None = None
    merge_pr: int | None = None
    pr_url: str | None = None
    reason: str | None = None


@dataclass
class TrustResult:
    trusted: bool
    commit_verdicts: dict[str, Verdict] = field(default_factory=dict)
    file_verdicts: dict[str, Verdict] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)
    # Verdict for any file not in ``file_verdicts``: it was last changed at or before the anchor.
    anchor_verdict: Verdict | None = None

    def verdict_for(self, path: str) -> Verdict:
        v = self.file_verdicts.get(path)
        if v is not None:
            return v
        if self.trusted and self.anchor_verdict is not None:
            return self.anchor_verdict
        return Verdict(DISPUTED, reason="no trust verdict for file")


def files_from_compare(compare: dict[str, Any]) -> list[str]:
    """Changed paths from a Forgejo compare result (``files[].filename``)."""
    return [f["filename"] for f in compare.get("files") or []]


def _commit_date(commit: dict[str, Any]) -> datetime | None:
    inner = commit.get("commit") or {}
    return parse_forgejo_ts((inner.get("committer") or {}).get("date") or commit.get("created"))


def _merge_verdict(
    sha: str,
    prs: list[dict[str, Any]],
    reviews: dict[int, list[dict[str, Any]]],
    bot_logins: set[str],
) -> Verdict:
    pr = next(
        (
            p
            for p in prs
            if p.get("merged")
            and p.get("merge_commit_sha") == sha
            and (p.get("base") or {}).get("ref") in (None, "main")
        ),
        None,
    )
    if pr is None:
        return Verdict(DISPUTED, reason="no merged pull request for merge commit")
    approvals = current_approvals(pr, reviews.get(pr["number"], []), bot_logins)
    if not approvals:
        return Verdict(
            DISPUTED,
            merge_pr=pr["number"],
            pr_url=pr.get("html_url"),
            reason="no approval of the final head",
        )
    names = tuple(sorted({r["user"]["login"] for r in approvals}))
    stamps = [parse_forgejo_ts(r.get("submitted_at")) for r in approvals]
    if any(t is None for t in stamps):
        return Verdict(DISPUTED, merge_pr=pr["number"], pr_url=pr.get("html_url"), reason=BAD_TIMESTAMP)
    return Verdict(TRUSTED, names, format_ts(max(t for t in stamps if t)), pr["number"], pr.get("html_url"))


def evaluate_chain(
    repo: str,
    commits: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    reviews: dict[int, list[dict[str, Any]]],
    changed_files: dict[str, list[str]],
    bot_logins: Iterable[str],
    anchor: str | None,
) -> TrustResult:
    bots = set(bot_logins)
    chain = list(reversed(commits))  # oldest first
    result = TrustResult(trusted=True)

    start = 0
    if anchor is not None:
        idx = next((i for i, c in enumerate(chain) if c["sha"] == anchor), None)
        if idx is None:
            result.trusted = False
            result.alerts.append(f"trust anchor {anchor} is not on main of {repo}")
            return result
        anchor_at = _commit_date(chain[idx])
        if anchor_at is None:
            result.alerts.append(f"trust anchor {anchor} of {repo} has no readable commit date")
            result.anchor_verdict = Verdict(DISPUTED, reason=BAD_TIMESTAMP)
        else:
            result.anchor_verdict = Verdict(
                TRUSTED, (TRUST_ANCHOR,), format_ts(anchor_at), reason="at or before the trust anchor"
            )
        start = idx + 1
        for c in chain[:start]:
            result.commit_verdicts[c["sha"]] = result.anchor_verdict
            for path in changed_files.get(c["sha"], []):
                result.file_verdicts[path] = result.anchor_verdict

    for c in chain[start:]:
        sha = c["sha"]
        if len(c.get("parents") or []) < 2:
            verdict = Verdict(DISPUTED, reason=NON_MERGE)
            result.trusted = False
            result.alerts.append(f"non-merge commit {sha} on main of {repo}")
        else:
            verdict = _merge_verdict(sha, prs, reviews, bots)
        result.commit_verdicts[sha] = verdict
        for path in changed_files.get(sha, []):
            result.file_verdicts[path] = verdict
            if verdict.status == DISPUTED and verdict.reason != NON_MERGE:
                alert = f"file {path} on {repo}/main has no approved merge PR"
                if alert not in result.alerts:
                    result.alerts.append(alert)

    if not result.trusted:
        for path, v in list(result.file_verdicts.items()):
            if v.status == TRUSTED:
                result.file_verdicts[path] = Verdict(DISPUTED, reason="repo is untrusted")
        result.anchor_verdict = None
    return result
