"""Render and verify the files, commit messages and pull request bodies agents write (plan 3.6)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import date
from typing import Any

import yaml

from knowledge_hub.agent.rules import Requester
from knowledge_hub.config import Config
from knowledge_hub.indexer.frontmatter import Rejected, parse_document

UNVERIFIED = "Written by an agent from the requester's instructions; the rationale below is not verified."


def render_file(
    *,
    id: str,
    title: str,
    domain: str,
    review_by: date,
    type: str,
    owner: str,
    entities: Sequence[str],
    body: str,
    status: str = "current",
    supersedes: Sequence[str] = (),
    superseded_by: Sequence[str] = (),
) -> str:
    data: dict[str, Any] = {
        "id": id,
        "title": title,
        "domain": domain,
        "review_by": review_by,
        "type": type,
        "owner": owner,
    }
    if status != "current":
        data["status"] = status
    for key, values in (("entities", entities), ("supersedes", supersedes), ("superseded_by", superseded_by)):
        if values:
            data[key] = list(values)
    dumped = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{dumped}---\n{body}\n"


def verify_roundtrip(text: str, path: str, config: Config, **expected: Any) -> None:
    parsed = parse_document(text, path, config.domains)
    if isinstance(parsed, Rejected):
        raise ValueError(f"rendered file does not parse: {parsed.reason}")
    for key, want in expected.items():
        got = parsed.body.strip("\n") if key == "body" else getattr(parsed, key)
        if isinstance(got, list):
            want = list(want)
        if got != want:
            raise ValueError(f"rendered file does not round-trip: {key}")


def trailers(tool: str, requester: Requester, proposal_id: str) -> str:
    return (
        f"Written-by: agent via knowledge-hub ({tool})\n"
        f"Requested-by: {requester.login} <{requester.email}> (sub: {requester.sub})\n"
        f"Proposal: {proposal_id}"
    )


def commit_message(subject: str, tool: str, requester: Requester, proposal_id: str) -> str:
    return f"{subject}\n\n{trailers(tool, requester, proposal_id)}"


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def pr_body(
    title: str,
    path: str,
    rationale: str,
    tool: str,
    requester: Requester,
    proposal_id: str,
    base_sha: str,
) -> str:
    fence = _fence(rationale)
    return (
        f"## What\n\nTitle: {title}\n\n`{path}`\n\n"
        f"## Why\n\n{UNVERIFIED}\n\n{fence}\n{rationale}\n{fence}\n\n"
        f"## Origin\n\n{trailers(tool, requester, proposal_id)}\nBase: {base_sha}\n"
    )


def content_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git_blob_sha(text: str) -> str:
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - git object id


def dedupe_key(sub: str, repo: str, path: str, kind: str, content_sha: str) -> str:
    return hashlib.sha256("\n".join((sub, repo, path, kind, content_sha)).encode("utf-8")).hexdigest()
