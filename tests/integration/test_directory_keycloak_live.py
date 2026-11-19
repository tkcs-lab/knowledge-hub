"""Fetch the `kh-test` realm from a real Keycloak (CI job `keycloak`); skipped without a server."""

from __future__ import annotations

import os

import pytest

from knowledge_hub.directory.keycloak import KeycloakProvider
from knowledge_hub.directory.rules import effective
from tests.support import keycloak_expected as exp

pytestmark = pytest.mark.integration

URL = os.environ.get("KH_TEST_KEYCLOAK_URL")
NEEDED = ("KH_TEST_KEYCLOAK_REALM", "KH_TEST_KEYCLOAK_CLIENT_ID", "KH_TEST_KEYCLOAK_CLIENT_SECRET")


@pytest.mark.skipif(not URL, reason="KH_TEST_KEYCLOAK_URL unset")
async def test_live_realm_matches_expectations():
    missing = [name for name in NEEDED if not os.environ.get(name)]
    assert not missing, f"missing {', '.join(missing)}"
    provider = KeycloakProvider(
        URL,
        os.environ["KH_TEST_KEYCLOAK_REALM"],
        os.environ["KH_TEST_KEYCLOAK_CLIENT_ID"],
        os.environ["KH_TEST_KEYCLOAK_CLIENT_SECRET"],
        False,
        None,
        env="test",
    )
    try:
        fetched = await provider.fetch(exp.REFS)
    finally:
        await provider.aclose()
    assert {u.email: (u.enabled, u.email_verified) for u in fetched["g"].members} == {
        u.email: (u.enabled, u.email_verified) for u in exp.expected_fetch()["g"].members
    }
    result = effective(exp.config(), fetched, exp.DOMAINS)
    assert result.members["g"] == exp.EXPECTED_MEMBERS
    assert result.approvers["g"] == exp.EXPECTED_APPROVERS
    assert {(s.role, s.who, s.reason) for s in result.skips} == exp.EXPECTED_SKIPS
