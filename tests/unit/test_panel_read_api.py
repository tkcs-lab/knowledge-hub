"""Panel read API: JSON shapes with the stats functions replaced (no database)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from knowledge_hub.config import ConfigError, GroupConfig, GroupDirectoryConfig, KhConfig, TechRepoConfig
from knowledge_hub.panel import stats
from knowledge_hub.panel.app import build_panel_app
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.settings import PanelSettings

pytestmark = pytest.mark.unit

YAML = "admins: [admin@example.test]\n"


def _config() -> KhConfig:
    return KhConfig(
        people={"alice": "alice@example.test"},
        labels={"ember": "Ember", "old": "Old"},
        archived_labels=["old"],
        tech_org="tech",
        tech_repos={"svc": TechRepoConfig(labels=["ember"], dirs=["docs"])},
        groups={
            "all": GroupConfig(members=["alice"], approvers=["alice"], slack_channel="#all"),
            "dir": GroupConfig(
                archived=True,
                directory={"keycloak": GroupDirectoryConfig(members=["eng"], approvers=["leads"])},
            ),
        },
    )


@pytest.fixture
def setup(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "kh.yaml"
    cfg_file.write_text(YAML)
    state: dict = {"load": _config}

    def load() -> KhConfig:
        return state["load"]()

    ctx = PanelContext(
        settings=PanelSettings(env="dev", config_file=cfg_file, dev_admin="admin@example.test"),
        signer=None,
        upstream=None,
        load_config=load,
        pool=object(),
    )

    async def group_stats(pool):
        s = stats.empty_group_stats()
        s["records"] = 3
        return {"all": s}

    async def label_usage(pool):
        return {"ember": {"documents": 2, "records": 1}}

    async def repo_state(pool):
        return {"kb-all": ("abc", "2026-01-01T00:00:00+00:00")}

    async def counts(pool):
        return {"kb-all": 4, "svc": 1}

    async def pipeline(pool):
        return {"jobs": {"queued": 1}}

    monkeypatch.setattr(stats, "group_stats", group_stats)
    monkeypatch.setattr(stats, "label_usage", label_usage)
    monkeypatch.setattr(stats, "repo_state", repo_state)
    monkeypatch.setattr(stats, "document_counts_by_repo", counts)
    monkeypatch.setattr(stats, "pipeline", pipeline)

    async def dataset_catalog(pool):
        return [{"name": "deals", "columns": [{"name": "stage"}], "rows_estimate": None}]

    monkeypatch.setattr(stats, "dataset_catalog", dataset_catalog)
    client = TestClient(build_panel_app(ctx), base_url="http://panel.test")
    return client, state, cfg_file


def test_me(setup):
    client, _, cfg_file = setup
    body = client.get("/api/me").json()
    assert body == {
        "email": "admin@example.test",
        "config_sha256": hashlib.sha256(YAML.encode()).hexdigest(),
    }


def test_labels(setup):
    client, _, _ = setup
    body = client.get("/api/labels").json()
    assert body["labels"] == [
        {"name": "ember", "description": "Ember", "archived": False, "documents": 2, "records": 1,
         "tech_repos": ["svc"]},
        {"name": "old", "description": "Old", "archived": True, "documents": 0, "records": 0,
         "tech_repos": []},
    ]
    assert len(body["config_sha256"]) == 64


def test_groups_with_directory_and_zero_stats(setup):
    client, _, _ = setup
    body = client.get("/api/groups").json()
    by_name = {g["name"]: g for g in body["groups"]}
    assert by_name["all"]["stats"]["records"] == 3
    assert by_name["all"]["slack_channel"] == "#all"
    assert by_name["all"]["members"] == ["alice"]
    assert by_name["dir"]["archived"] is True
    assert by_name["dir"]["directory"] == {"keycloak": {"members": ["eng"], "approvers": ["leads"]}}
    assert by_name["dir"]["stats"] == stats.empty_group_stats()
    assert by_name["dir"]["stats"]["documents"] == {"T0": 0, "T0a": 0}
    assert body["people"] == [{"login": "alice", "email": "alice@example.test"}]


def test_repos(setup):
    client, _, _ = setup
    body = client.get("/api/repos").json()
    assert body["tech_org"] == "tech"
    rows = {r["name"]: r for r in body["repos"]}
    assert rows["kb-all"] == {
        "name": "kb-all", "kind": "group", "group": "all", "dirs": None, "labels": None,
        "archived": False, "documents": 4, "last_indexed_sha": "abc",
        "indexed_at": "2026-01-01T00:00:00+00:00",
    }
    assert rows["svc"]["kind"] == "tech"
    assert rows["svc"]["dirs"] == ["docs"] and rows["svc"]["labels"] == ["ember"]
    assert rows["svc"]["last_indexed_sha"] is None
    assert rows["kb-dir"]["archived"] is True


def test_datasets(setup):
    client, _, _ = setup
    body = client.get("/api/datasets").json()
    assert body == {
        "datasets": [{"name": "deals", "columns": [{"name": "stage"}], "rows_estimate": None}]
    }


def test_pipeline(setup):
    client, _, _ = setup
    assert client.get("/api/pipeline").json() == {"jobs": {"queued": 1}}


@pytest.mark.parametrize("path", ["/api/labels", "/api/groups", "/api/repos"])
def test_invalid_config_is_503(setup, path):
    client, state, _ = setup

    def bad() -> KhConfig:
        raise ConfigError("config: broken")

    state["load"] = bad
    resp = client.get(path)
    assert resp.status_code == 503
    assert resp.json() == {"error": "config_invalid", "detail": "config: broken"}
