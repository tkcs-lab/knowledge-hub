import hashlib
import json

import pytest

from knowledge_hub.config import Config, ConfigError, EntityKind, load_config, parse_config
from knowledge_hub.indexer.rebuild import config_hash

pytestmark = pytest.mark.unit


def test_default_config():
    c = load_config()
    assert c.audiences == ["all", "mgmt", "hr", "contracts"]
    assert c.domains == ["portfolio", "product", "technical"]
    assert c.repos["notes-all"].tier == "T1"
    assert c.repos["canonical-hr"].audience == "hr"
    assert c.ingest_principal_types == ["n8n_workflow"]
    assert c.entities["cus"].audiences == ["contracts"] and c.entities["prd"].table == "Products"
    assert c.restricted_audiences_imply_all is False


def test_unknown_audience():
    with pytest.raises(ConfigError, match="^config:"):
        parse_config({"raw_audience_map": {"series:1": ["nobody"]}})


def test_repo_audience_not_listed():
    with pytest.raises(ConfigError, match="^config:"):
        parse_config({"audiences": ["all"], "repos": {"r": {"tier": "T0", "audience": "hr"}}})


def test_two_t0_repos_one_audience():
    repos = {"a": {"tier": "T0", "audience": "all"}, "b": {"tier": "T0", "audience": "all"}}
    with pytest.raises(ConfigError, match="^config:.*more than one T0"):
        parse_config({"repos": repos})


def test_ingest_principal_service_refused():
    with pytest.raises(ConfigError, match="^config:"):
        parse_config({"ingest_principal_types": ["service"]})


def test_load_from_file(tmp_path):
    p = tmp_path / "kh.yaml"
    p.write_text("domains: [product]\n")
    assert load_config(p).domains == ["product"]


def test_entities_legacy_list_form():
    c = parse_config({"entities": {"prd": ["all"]}})
    assert c.entities == {"prd": EntityKind(table="Products", audiences=["all"], label="product")}


def test_entities_custom_kind():
    fam = {"table": "Family", "audiences": ["all"], "label": "family member"}
    c = parse_config({"entities": {"fam": fam}})
    assert list(c.entities) == ["fam"]
    spec = c.entities["fam"]
    assert (spec.table, spec.audiences, spec.label) == ("Family", ["all"], "family member")


_OK = {"table": "Family", "audiences": ["all"]}


@pytest.mark.parametrize(
    ("entities", "fragment"),
    [
        ({"fam": ["all"]}, "the list form is only for"),
        ({}, "at least one kind"),
        ({f"k{i}": {"table": f"T{i}", "audiences": ["all"]} for i in range(33)}, "more than 32 kinds"),
        ({"Fam": _OK}, "must be 1-16 lowercase"),
        ({"doc": _OK}, "is reserved"),
        ({"fam": {"table": "Fam/../x", "audiences": ["all"]}}, "must be letters, digits"),
        ({"a": _OK, "b": _OK}, "use the same table"),
        ({"fam": {**_OK, "label": "a\nb"}}, "label must be 1-40"),
        ({"fam": {"table": "Family", "audiences": ["nobody"]}}, "unknown audience"),
    ],
)
def test_entities_rejected(entities, fragment):
    with pytest.raises(ConfigError, match=r"^config: .*" + fragment):
        parse_config({"entities": entities})


def test_config_hash_unchanged_for_default_entities():
    payload = {
        "raw_audience_map": {},
        "repos": {name: r.audience for name, r in sorted(Config().repos.items())},
        "entities": {"cmp": ["all"], "cus": ["contracts"], "prd": ["all"], "ver": ["all"]},
    }
    old = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    lists = {"prd": ["all"], "cmp": ["all"], "ver": ["all"], "cus": ["contracts"]}
    legacy = parse_config({"entities": lists})
    assert old == config_hash(Config()) == config_hash(legacy)
