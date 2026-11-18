"""list_audiences: PrivacyFence's audiences source; every other caller is refused."""

from __future__ import annotations

import json

import pytest

from knowledge_hub.auth.claims import AUDIENCE_RE, Caller
from tests.support.principals import KH_CONFIG, alice_all, wf_token

pytestmark = pytest.mark.integration

PF = Caller(sub="privacyfence", principal_type="service", email=None, audiences=frozenset(), token_id=None)

ARCHIVED_MGMT = KH_CONFIG.model_copy(deep=True)
ARCHIVED_MGMT.groups["mgmt"].archived = True


async def test_privacyfence_gets_the_audience_table(mcp_client):
    res = await mcp_client.call(PF, "list_audiences", {})
    assert not res.is_error, res.text
    assert res.data == {
        "version": 1,
        "audiences": {
            "all": ["alice@example.test", "multi@example.test"],
            "hr": ["carol@example.test"],
            "mgmt": ["bob@example.test", "multi@example.test"],
        },
    }
    assert json.loads(res.text) == res.data


async def test_audience_table_has_the_privacyfence_shape(mcp_client):
    res = await mcp_client.call(PF, "list_audiences", {})
    assert set(res.data) == {"version", "audiences"}
    assert type(res.data["version"]) is int
    for name, addresses in res.data["audiences"].items():
        assert AUDIENCE_RE.fullmatch(name)
        assert isinstance(addresses, list) and all(isinstance(a, str) for a in addresses)


@pytest.mark.parametrize("who", [alice_all, wf_token], ids=lambda c: c.sub)
async def test_a_person_or_workflow_is_refused(mcp_client, who):
    res = await mcp_client.call(who, "list_audiences", {})
    assert res.is_error
    assert res.text.endswith("this tool answers only PrivacyFence")


async def test_extra_arguments_are_refused(mcp_client):
    res = await mcp_client.call(PF, "list_audiences", {"x": 1})
    assert res.is_error


@pytest.mark.parametrize("kh_config", [ARCHIVED_MGMT])
async def test_an_archived_group_is_left_out(mcp_client, kh_config):
    res = await mcp_client.call(PF, "list_audiences", {})
    assert not res.is_error, res.text
    assert "mgmt" not in res.data["audiences"]
    assert set(res.data["audiences"]) == {"all", "hr"}


async def test_a_service_claim_still_gets_no_data_from_search(mcp_client):
    res = await mcp_client.call(PF, "search", {"query": "policy"})
    assert res.is_error
    assert res.status == 401
