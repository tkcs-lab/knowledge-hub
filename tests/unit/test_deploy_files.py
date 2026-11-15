"""Static checks of the deployment files (the image build itself is a manual step)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

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
