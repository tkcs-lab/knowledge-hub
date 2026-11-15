"""PrivacyFence skips upstream tools whose schema it cannot forward (plan 3.2); ours must all pass."""

import json

from knowledge_hub.config import Config
from knowledge_hub.mcp.server import Deps, build_mcp
from knowledge_hub.settings import Settings

MAX_SCHEMA_BYTES = 32 * 1024
FORBIDDEN_KEYS = ("$ref", "$defs", "definitions")


def _walk(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


async def test_every_registered_tool_passes_privacyfence_skip_rules():
    deps = Deps(settings=Settings(env="test"), config=Config(), app_pool=None, indexer_pool=None)
    tools = await build_mcp(deps).list_tools()
    assert "get_doc" in {t.name for t in tools}
    for tool in tools:
        schema = tool.input_schema
        assert schema.get("type") == "object", tool.name
        keys = set(_walk(schema))
        assert not keys & set(FORBIDDEN_KEYS), tool.name
        assert "reason" not in schema.get("properties", {}), tool.name
        assert len(json.dumps(schema).encode()) < MAX_SCHEMA_BYTES, tool.name
