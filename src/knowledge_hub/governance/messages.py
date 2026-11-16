"""Finding codes and their fixed texts. No text names a repo, a path or a value from elsewhere."""

from __future__ import annotations

import enum
from dataclasses import dataclass

MAX_FILE_BYTES = 1048576
MAX_FILES = 200


class Code(enum.Enum):
    LAYOUT = "path does not match the repository layout"
    PARSE = "{}"
    UNKNOWN_KEY = "unknown frontmatter key {}"
    DOMAIN = "domain must be {}"
    ID_FORMAT = "id must use lowercase letters, digits and single hyphens"
    ID_FILENAME = "id must be {}"
    ID_IN_USE = "id already in use"
    TYPE_MISSING = "type is required"
    TYPE_PATH = "type must be {} for this path"
    OWNER_MISSING = "owner is required"
    OWNER = "owner must be {}"
    ENTITY_FORMAT = "entity {} is not a valid reference"
    ENTITY_NOT_ALLOWED = "entity {} is not allowed in this repository"
    ENTITY_NOT_FOUND = "entity not found: {}"
    FOLDER_UNKNOWN = "product or component folder is not a known entity"
    SUPERSEDES = "supersedes and superseded_by must name another document of this repository"
    TOO_LARGE = "file is larger than 1 MiB"
    TOO_MANY = "more than 200 markdown files changed"


@dataclass(frozen=True)
class Finding:
    path: str
    code: Code
    detail: str | None = None


def render_line(f: Finding) -> str:
    text = f.code.value
    if "{}" in text:
        text = text.format(f.detail)
    return f"{f.path}: {text}"


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.path, f.code.name, f.detail or ""))
