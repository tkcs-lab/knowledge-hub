"""The redirect allowlist refuses look-alikes."""

from __future__ import annotations

import pytest

from knowledge_hub.oauth.allowlist import redirect_allowed

ALLOW = ["https://claude.ai/api/mcp/auth_callback", "http://localhost", "http://127.0.0.1"]


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback/x",
        "https://claude.ai/api/mcp/auth_callback?a=1",
        "https://claude.ai.evil.example/api/mcp/auth_callback",
        "http://claude.ai/api/mcp/auth_callback",
        "http://localhost.evil.example/cb",
        "http://localhost@evil.example/cb",
        "http://127.0.0.1.evil.example/",
        "https://evil.example/#http://localhost",
        "javascript:alert(1)",
        "http://localhost/cb#frag",
        "http://user:pw@localhost/cb",
        "http://localhost:notaport/cb",
        "",
    ],
)
def test_redirect_allowlist_refuses_lookalikes(uri):
    assert not redirect_allowed(uri, ALLOW)


@pytest.mark.parametrize(
    "uri",
    ["http://localhost:1/a/b", "http://127.0.0.1:65535/", "https://claude.ai/api/mcp/auth_callback"],
)
def test_redirect_allowlist_accepts_localhost_any_port_path(uri):
    assert redirect_allowed(uri, ALLOW)
