"""The PrivacyFence declaration example matches the registered tools and PrivacyFence's loader rules."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from knowledge_hub.config import KhConfig
from knowledge_hub.mcp.server import Deps, build_mcp
from knowledge_hub.settings import Settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
READ_TOOLS: set[str] = {"search", "get_doc", "list_stale", "list_labels", "usage"}
WRITE_TOOLS: set[str] = {"ingest_record", "propose_doc"}
READ_VERBS = {"read", "download", "search"}
VERBS = READ_VERBS | {"add", "update", "restructure", "move", "send", "delete"}

DECLARATION = yaml.safe_load((ROOT / "deploy" / "privacyfence-upstream.example.yaml").read_text())
COMPOSE = yaml.safe_load((ROOT / "deploy" / "compose.example.yaml").read_text())
UPSTREAM = DECLARATION["upstreams"][0]
TOOLS = UPSTREAM["tools"]


async def test_every_registered_tool_is_declared():
    deps = Deps(settings=Settings(env="test"), config=KhConfig(), app_pool=None, indexer_pool=None)
    registered = {t.name for t in await build_mcp(deps).list_tools()}
    assert registered == set(TOOLS), "declare every registered tool in the upstream declaration"
    assert registered == READ_TOOLS | WRITE_TOOLS, (
        "a new tool must be classified as read or write here and declared"
    )


def test_write_tools_are_not_auto():
    for name in WRITE_TOOLS:
        assert TOOLS[name]["gate"] == "popup", name
    for name in READ_TOOLS:
        assert TOOLS[name]["gate"] == "auto", name


def test_tool_rules_follow_the_privacyfence_loader():
    for name, tool in TOOLS.items():
        assert set(tool) <= {"gate", "verb", "effect"}, name
        assert tool["verb"] in VERBS, name
        if tool["verb"] in READ_VERBS:
            assert tool["gate"] in {"auto", "review"}, name
            assert "effect" not in tool, name
            assert name in READ_TOOLS, name
        else:
            assert tool["gate"] == "popup", name
            effect = tool.get("effect", "")
            assert 1 <= len(effect) <= 200 and effect.endswith("."), name
            assert name in WRITE_TOOLS, name


def test_identity_declaration():
    assert UPSTREAM["identity_claim"] == "required"
    assert UPSTREAM["allowed_principal_types"] == ["claude_client", "n8n_workflow"]
    assert UPSTREAM["email_for_visibility"] is True


def test_upstream_fields():
    assert set(DECLARATION) <= {"version", "upstreams", "claim", "membership"}
    assert DECLARATION["version"] == 1
    assert len(DECLARATION["upstreams"]) == 1
    allowed = {
        "id", "label", "url", "audience", "tools", "identity_claim",
        "claim_lifetime_seconds", "allowed_principal_types", "email_for_visibility",
    }
    assert set(UPSTREAM) <= allowed
    assert UPSTREAM["id"] == "kh" and re.fullmatch(r"[a-z][a-z0-9]{1,15}", UPSTREAM["id"])
    assert UPSTREAM["url"] == "http://knowledge-hub:8080/mcp"
    env = COMPOSE["services"]["knowledge-hub"]["environment"]
    assert UPSTREAM["audience"] == env["KH_PF_AUDIENCE"]
    assert UPSTREAM["identity_claim"] == "required"
    assert UPSTREAM["allowed_principal_types"] == ["claude_client", "n8n_workflow"]
    assert "knowledge-hub:8080" in env["KH_ALLOWED_HOSTS"].split(",")


def test_header_explains_people():
    header = "\n".join(
        line
        for line in (ROOT / "deploy" / "privacyfence-upstream.example.yaml").read_text().splitlines()
        if line.startswith("#")
    )
    assert "people" in header


def test_header_names_membership_and_the_n8n_map():
    header = []
    for line in (ROOT / "deploy" / "privacyfence-upstream.example.yaml").read_text().splitlines():
        if not line.startswith("#"):
            break
        header.append(line)
    text = "\n".join(header)
    assert "people" in text
    assert "membership" in text
    assert "upstream_mcp.kh.groups" in text
    assert "membership" not in DECLARATION, "the example keeps the default snapshot source"
