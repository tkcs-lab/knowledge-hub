"""Static checks of the deployment files (the image build itself is a manual step)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from knowledge_hub.config import load_config

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "backup-kh.sh"
TABLES = [
    "raw_records",
    "raw_audience_map",
    "derived_records",
    "derived_sources",
    "candidates",
    "candidate_sources",
    "conflicts",
    "conflict_items",
]


def test_example_config_loads():
    cfg = load_config(ROOT / "deploy" / "kh.example.yaml")
    assert cfg.repos["canonical-all"].tier == "T0"
    assert cfg.repos["notes-all"].tier == "T1"


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
    env = data["services"]["knowledge-hub"]["environment"]
    for key, value in env.items():
        if re.search(r"(PASSWORD|TOKEN|KEY|HMAC)", key):
            assert key.endswith("_FILE"), key
            assert str(value).startswith("/run/secrets/"), key
    assert not re.search(r"(?i)(password|token|secret)\s*[:=]\s*[\"']?[A-Za-z0-9+/]{16,}", text)


COMPOSE = ROOT / "deploy" / "compose.example.yaml"


def _compose():
    return yaml.safe_load(COMPOSE.read_text())


def test_compose_example_claim_settings():
    env = _compose()["services"]["knowledge-hub"]["environment"]
    issuer = urlparse(env["KH_PF_ISSUER"])
    jwks = urlparse(env["KH_PF_JWKS_URL"])
    assert issuer.scheme == "https" and issuer.path == ""
    assert jwks.scheme == "http"
    assert jwks.path == "/upstream-mcp/jwks"
    assert jwks.hostname == issuer.hostname
    assert "well-known" not in COMPOSE.read_text()


def test_compose_example_networks():
    data = _compose()
    used = set()
    for svc in data["services"].values():
        used |= set(svc.get("networks", []))
    for name in used:
        assert data["networks"][name]["internal"] is True
    assert "pf-kh" in data["services"]["knowledge-hub"]["networks"]


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
