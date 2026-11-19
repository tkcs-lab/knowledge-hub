"""Static checks of the deployment files (the image build itself is a manual step)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from knowledge_hub.config import load_config
from knowledge_hub.panel.settings import PanelSettings

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "backup-kh.sh"
TABLES = ["records"]


def test_example_config_loads():
    cfg = load_config(ROOT / "deploy" / "kh.example.yaml")
    assert set(cfg.groups) == {"all", "mgmt"}
    assert cfg.repo_for_group("all") == "kb-all" and cfg.repo_for_group("mgmt") == "kb-mgmt"
    assert len(cfg.labels) == 2
    assert {m for g in cfg.groups.values() for m in g.members} <= set(cfg.people)


def test_dockerfile():
    text = (ROOT / "Dockerfile").read_text()
    users = re.findall(r"^USER\s+(\S+)", text, re.M)
    assert users and users[-1] not in ("root", "0")
    assert "latest" not in text.lower()
    assert re.search(r'^ENTRYPOINT\s+\["kh"\]', text, re.M)
    assert re.search(r'^CMD\s+\["serve"\]', text, re.M)


def test_backup_script_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_backup_script_content():
    text = SCRIPT.read_text()
    for t in TABLES:
        assert re.search(rf"\b{t}\b", text), t
    assert "--enable-row-security" in text
    assert "--inserts" in text
    assert "set -euo pipefail" in text
    assert not re.search(r"\b(documents|chunks)\b", re.sub(r"^\s*#.*$", "", text, flags=re.M))
    assert not re.search(r"-t\s+\S*\b(documents|chunks)\b", text)


def test_compose_example_has_no_secret_values():
    text = (ROOT / "deploy" / "compose.example.yaml").read_text()
    data = yaml.safe_load(text)
    for name in ("knowledge-hub", "knowledge-hub-directory", "knowledge-hub-panel"):
        env = data["services"][name]["environment"]
        for key, value in env.items():
            if re.search(r"(PASSWORD|TOKEN|KEY|HMAC|SECRET|CREDENTIALS)", key):
                assert key.endswith("_FILE"), key
                assert str(value).startswith("/run/secrets/"), key
    assert not re.search(r"(?i)(password|token|secret)\s*[:=]\s*[\"']?[A-Za-z0-9+/]{16,}", text)


COMPOSE = ROOT / "deploy" / "compose.example.yaml"


def _compose():
    return yaml.safe_load(COMPOSE.read_text())


def test_compose_example_networks():
    data = _compose()
    used = set()
    for svc in data["services"].values():
        used |= set(svc.get("networks", []))
    for name in used:
        if name != "egress":
            assert data["networks"][name]["internal"] is True
    assert not (data["networks"]["egress"] or {}).get("internal", False)
    assert set(data["networks"]) == {"db", "kh", "egress"}
    assert used == {"db", "kh", "egress"}
    assert "egress" not in data["services"]["ollama"]["networks"]


def test_compose_example_directory_service():
    data = _compose()
    hub = data["services"]["knowledge-hub"]
    svc = data["services"]["knowledge-hub-directory"]
    assert svc["image"] == hub["image"]
    assert svc["command"] == ["directory", "sync", "--every", "300"]
    assert set(svc["networks"]) == {"db", "egress"}
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
    assert set(svc["secrets"]) == {"pg_kh_indexer", "kh_directory_google_key", "kh_directory_keycloak_secret"}
    # Only the sync holds a directory credential.
    assert "kh_directory_google_key" not in hub["secrets"]
    assert "kh_directory_keycloak_secret" not in hub["secrets"]
    assert "kh_oidc_client_secret" in hub["secrets"]
    assert "kh_oidc_client_secret" not in svc["secrets"]
    for name in ("kh_oidc_client_secret", "kh_directory_google_key", "kh_directory_keycloak_secret"):
        assert data["secrets"][name] == {"file": f"/etc/stack-secrets/{name}"}
    assert "KH_OIDC_CLIENT_SECRET_FILE: /run/secrets/kh_oidc_client_secret" in COMPOSE.read_text()
    assert not [k for k in hub["environment"] if k.startswith("KH_PF_")]
    assert "pf-kh" not in data["networks"]


def test_compose_example_ollama_and_image():
    data = _compose()
    hub_image = data["services"]["knowledge-hub"]["image"]
    assert re.fullmatch(r"ghcr\.io/tkcs-lab/knowledge-hub:\d+\.\d+\.\d+", hub_image)
    ollama = data["services"]["ollama"]
    assert re.fullmatch(r"ollama/ollama:\d+\.\d+\.\d+", ollama["image"])
    assert ollama["mem_limit"] == "3g"
    env = ollama["environment"]
    assert env["OLLAMA_MAX_LOADED_MODELS"] == "1"
    assert env["OLLAMA_NUM_PARALLEL"] == "1"
    assert env["OLLAMA_KEEP_ALIVE"] == "-1"
    assert data["volumes"]["ollama-models"]["name"] == "kh-ollama-models"


def test_compose_example_config_passes():
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        pytest.skip("docker compose is not available")
    result = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "config", "-q"], cwd=ROOT)
    assert result.returncode == 0


def test_panel_service_is_wired(tmp_path, monkeypatch):
    svc = _compose()["services"]["knowledge-hub-panel"]
    assert svc["command"] == ["panel", "serve", "--port", "8081"]
    assert "ports" not in svc
    assert svc["restart"]
    assert "./config:/etc/kh" in svc["volumes"]
    assert not any(str(v).startswith("./config:/etc/kh") and str(v).endswith(":ro") for v in svc["volumes"])
    env = svc["environment"]
    assert not [k for k in env if k.startswith("KH_OIDC_") or k == "KH_PUBLIC_URL"]
    for key in (
        "KH_PANEL_PUBLIC_URL",
        "KH_PANEL_OIDC_ISSUER",
        "KH_PANEL_OIDC_CLIENT_ID",
        "KH_PANEL_OIDC_CLIENT_SECRET_FILE",
        "KH_PANEL_OIDC_ALLOWED_DOMAINS",
        "KH_PANEL_OIDC_HOSTED_DOMAINS",
        "KH_PANEL_SESSION_KEY_FILE",
    ):
        assert key in env, key
    for key in list(env):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if key.endswith("_FILE"):
            f = tmp_path / key
            f.write_text("x" * 40)
            value = str(f)
        monkeypatch.setenv(key, str(value))
    settings = PanelSettings()
    assert settings.allowed_domains_tuple == ("example.com",)
    assert settings.read_session_key() == b"x" * 40


def test_example_config_has_admins():
    cfg = load_config(ROOT / "deploy" / "kh.example.yaml")
    assert isinstance(cfg.admins, list) and cfg.admins
