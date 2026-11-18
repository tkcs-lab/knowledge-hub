"""Frontmatter parsing and validation (plan section 3.6)."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from knowledge_hub.config import LABEL_RE


class _Loader(yaml.SafeLoader):
    """Safe loader that leaves ``2027-01-31`` a string, so a bad date is ``invalid review_by``."""


_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")
MAX_SUPERSEDES = 20
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class ParsedDoc:
    id: str
    title: str
    review_by: dt.date
    body: str
    labels: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Rejected:
    reason: str


def _split(text: str) -> tuple[str, str] | None:
    text = text.lstrip("﻿")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1 :])
    return None


def strip_frontmatter(text: str) -> str:
    """The text after a closed leading ``---`` block, or ``text`` unchanged; the block is not parsed."""
    parts = _split(text)
    return text if parts is None else parts[1]


def _review_by(value: Any) -> dt.date | None:
    if isinstance(value, str) and _DATE_RE.fullmatch(value):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def parse_document(text: str, path: str) -> ParsedDoc | Rejected:
    """Parse one Markdown file. ``path`` is for the caller's messages; it takes no part in the rules."""
    del path
    parts = _split(text)
    if parts is None:
        return Rejected("missing frontmatter")
    raw, body = parts
    try:
        data = yaml.load(raw, Loader=_Loader)  # noqa: S506 (safe subclass)
    except (yaml.YAMLError, ValueError):
        return Rejected("invalid frontmatter")
    if not isinstance(data, dict):
        return Rejected("invalid frontmatter")

    for key in ("id", "title", "review_by"):
        if data.get(key) is None:
            return Rejected(f"missing {key}")

    doc_id = data["id"]
    if not isinstance(doc_id, str) or not ID_RE.fullmatch(doc_id):
        return Rejected("invalid id")
    title = data["title"]
    if not isinstance(title, str) or not title.strip():
        return Rejected("invalid title")
    review_by = _review_by(data["review_by"])
    if review_by is None:
        return Rejected("invalid review_by")

    labels = data.get("labels")
    if labels is None:
        labels = []
    elif not isinstance(labels, list) or not all(
        isinstance(x, str) and LABEL_RE.fullmatch(x) for x in labels
    ):
        return Rejected("invalid labels")

    supersedes = data.get("supersedes")
    if supersedes is None:
        supersedes = []
    elif isinstance(supersedes, str):
        supersedes = [supersedes]
    if (
        not isinstance(supersedes, list)
        or len(supersedes) > MAX_SUPERSEDES
        or not all(isinstance(x, str) and ID_RE.fullmatch(x) for x in supersedes)
        or len(set(supersedes)) != len(supersedes)
        or doc_id in supersedes
    ):
        return Rejected("invalid supersedes")

    return ParsedDoc(
        id=doc_id, title=title.strip(), review_by=review_by, body=body, labels=list(labels),
        supersedes=list(supersedes),
    )  # fmt: skip
