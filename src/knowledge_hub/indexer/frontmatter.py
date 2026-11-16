"""Frontmatter parsing and validation (plan section 3.6)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import yaml

FORBIDDEN_KEYS = (
    "tier",
    "audience",
    "audiences",
    "required_audiences",
    "allowed_emails",
    "visibility",
    "approved_by",
    "approved_at",
    "generated_by",
    "derived_from",
)
REQUIRED_KEYS = ("id", "title", "domain", "review_by")
OPTIONAL_KEYS = ("type", "owner", "status", "entities", "supersedes", "superseded_by")
ALLOWED_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS
ALLOWED_STATUS = ("current", "superseded")


@dataclass(frozen=True)
class ParsedDoc:
    path: str
    id: str
    title: str
    domain: str
    review_by: dt.date
    body: str
    owner: str | None = None
    type: str | None = None
    entities: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    superseded_by: list[str] = field(default_factory=list)
    status: str = "current"


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


def raw_keys(text: str) -> list[str] | None:
    """The frontmatter keys as written, or None when the block is missing or not a mapping."""
    split = _split(text)
    if split is None:
        return None
    try:
        data = yaml.safe_load(split[0])
    except (yaml.YAMLError, ValueError):
        return None
    return list(data) if isinstance(data, dict) else None


def _str_list(value: Any, key: str) -> list[str] | str:
    """Return a list of strings, or an error text."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) and v for v in value):
        return list(value)
    return f"frontmatter: {key} must be a string or a list of strings"


def _opt_str(value: Any, key: str) -> str | None | Rejected:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return Rejected(f"frontmatter: {key} must be a string")


def parse_document(text: str, path: str, domains: list[str]) -> ParsedDoc | Rejected:
    parts = _split(text)
    if parts is None:
        return Rejected("frontmatter: missing or unterminated frontmatter block")
    raw, body = parts
    try:
        data = yaml.safe_load(raw)
    except (yaml.YAMLError, ValueError):
        return Rejected("frontmatter: invalid YAML or date")
    if not isinstance(data, dict):
        return Rejected("frontmatter: must be a mapping")

    for key in FORBIDDEN_KEYS:
        if key in data:
            return Rejected(f"frontmatter must not set {key}; the server decides tier and audience")

    missing = [k for k in REQUIRED_KEYS if data.get(k) in (None, "")]
    if missing:
        return Rejected(f"frontmatter: missing required key {', '.join(missing)}")

    doc_id = data["id"]
    if isinstance(doc_id, int) and not isinstance(doc_id, bool):
        doc_id = str(doc_id)
    if not isinstance(doc_id, str) or not doc_id.strip():
        return Rejected("frontmatter: id must be a string")
    title = data["title"]
    if not isinstance(title, str) or not title.strip():
        return Rejected("frontmatter: title must be a string")
    domain = data["domain"]
    if not isinstance(domain, str) or domain not in domains:
        return Rejected(f"frontmatter: unknown domain {domain}")

    rb = data["review_by"]
    if isinstance(rb, dt.datetime):
        return Rejected("frontmatter: review_by must be a date (YYYY-MM-DD)")
    if isinstance(rb, str):
        try:
            rb = dt.date.fromisoformat(rb)
        except ValueError:
            return Rejected("frontmatter: review_by must be a date (YYYY-MM-DD)")
    if not isinstance(rb, dt.date):
        return Rejected("frontmatter: review_by must be a date (YYYY-MM-DD)")

    status = data.get("status", "current")
    if status == "disputed":
        return Rejected("frontmatter: status disputed cannot be set by a file")
    if status not in ALLOWED_STATUS:
        return Rejected("frontmatter: status must be current or superseded")

    owner = _opt_str(data.get("owner"), "owner")
    if isinstance(owner, Rejected):
        return owner
    typ = _opt_str(data.get("type"), "type")
    if isinstance(typ, Rejected):
        return typ

    lists: dict[str, list[str]] = {}
    for key in ("entities", "supersedes", "superseded_by"):
        got = _str_list(data.get(key), key)
        if isinstance(got, str):
            return Rejected(got)
        lists[key] = got

    return ParsedDoc(
        path=path,
        id=doc_id.strip(),
        title=title.strip(),
        domain=domain,
        review_by=rb,
        body=body,
        owner=owner,
        type=typ,
        status=status,
        **lists,
    )
