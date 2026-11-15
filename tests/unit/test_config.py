import pytest

from knowledge_hub.config import ConfigError, load_config, parse_config

pytestmark = pytest.mark.unit


def test_default_config():
    c = load_config()
    assert c.audiences == ["all", "mgmt", "hr", "contracts"]
    assert c.domains == ["portfolio", "product", "technical"]
    assert c.repos["notes-all"].tier == "T1"
    assert c.repos["canonical-hr"].audience == "hr"
    assert c.ingest_principal_types == ["n8n_workflow"]
    assert c.entities["cus"] == ["contracts"] and c.entities["prd"] == ["all"]
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
