"""JSON Schema (2020-12) of the canonical-repo frontmatter."""

from __future__ import annotations

import json
from typing import Any

from knowledge_hub.governance import TYPE_FOLDERS
from knowledge_hub.indexer.frontmatter import ALLOWED_KEYS, ALLOWED_STATUS, FORBIDDEN_KEYS, REQUIRED_KEYS

_ID_LIST: dict[str, Any] = {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}


def build_schema() -> dict[str, Any]:
    props: dict[str, Any] = {
        "id": {"type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"},
        "title": {"type": "string", "minLength": 1},
        "domain": {"enum": ["portfolio", "product", "technical"]},
        "review_by": {"type": "string", "format": "date"},
        "type": {"enum": sorted(set(TYPE_FOLDERS.values()) | {"glossary"})},
        "owner": {"type": "string"},
        "status": {"enum": list(ALLOWED_STATUS)},
        "entities": {
            "type": "array",
            "items": {"type": "string", "pattern": "^(prd|cmp|ver|cus):[a-z0-9.-]+$"},
        },
        "supersedes": dict(_ID_LIST),
        "superseded_by": dict(_ID_LIST),
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "knowledge-hub frontmatter",
        "type": "object",
        "properties": {k: props[k] for k in ALLOWED_KEYS},
        "required": sorted(REQUIRED_KEYS + ("type",)),
        "additionalProperties": False,
        "x-forbidden-keys": list(FORBIDDEN_KEYS),
    }


def dump_schema() -> str:
    return json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"
