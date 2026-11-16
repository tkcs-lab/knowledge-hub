import datetime as dt
import json

import pytest

from knowledge_hub.governance.schema import build_schema, dump_schema
from knowledge_hub.indexer.frontmatter import (
    ALLOWED_KEYS,
    ALLOWED_STATUS,
    FORBIDDEN_KEYS,
    REQUIRED_KEYS,
    ParsedDoc,
)

pytestmark = pytest.mark.unit


def test_properties_match_allowed_keys():
    assert set(build_schema()["properties"]) == set(ALLOWED_KEYS)


def test_required_and_strictness():
    schema = build_schema()
    assert set(schema["required"]) == set(REQUIRED_KEYS) | {"type"}
    assert schema["additionalProperties"] is False
    assert schema["x-forbidden-keys"] == list(FORBIDDEN_KEYS)
    assert schema["properties"]["status"]["enum"] == list(ALLOWED_STATUS)


def test_no_key_is_allowed_and_forbidden():
    assert not set(ALLOWED_KEYS) & set(FORBIDDEN_KEYS)


def test_dump_is_deterministic_json():
    text = dump_schema()
    assert text.endswith("\n")
    assert json.loads(text) == build_schema()


def test_parsed_doc_fields_use_only_schema_properties():
    doc = ParsedDoc(
        path="product/a.md", id="a", title="A", domain="product", review_by=dt.date(2030, 1, 1), body="x"
    )
    document = {
        k: getattr(doc, k)
        for k in ALLOWED_KEYS
    }
    assert set(document) <= set(build_schema()["properties"])
