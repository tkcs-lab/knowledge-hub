"""list_groups and list_repos: open to every kh:read caller, also one in no group."""

from __future__ import annotations

import pytest

from tests.support.principals import KH_CONFIG, alice_all, dave_none, wf_token

pytestmark = pytest.mark.integration

CALLERS = [alice_all, dave_none, wf_token]


@pytest.mark.parametrize("tool", ["list_groups", "list_repos"])
async def test_same_answer_for_every_caller(mcp_client, seeded_db, tool):
    answers = []
    for who in CALLERS:
        res = await mcp_client.call(who, tool)
        assert not res.is_error, res.text
        answers.append(res.data)
    assert answers[0] == answers[1] == answers[2]


async def test_list_groups_names_every_active_group(mcp_client, seeded_db):
    res = await mcp_client.call(dave_none, "list_groups")
    names = [g["name"] for g in res.data["result"]["groups"]]
    assert names == sorted(KH_CONFIG.active_groups)


async def test_list_repos_counts_the_seeded_index(mcp_client, seeded_db):
    res = await mcp_client.call(dave_none, "list_repos")
    by = {r["name"]: r for r in res.data["result"]["repos"]}
    for repo in ("kb-all", "kb-mgmt", "kb-hr"):
        assert by[repo]["documents"] == 1
        assert by[repo]["last_indexed_sha"] is None
        assert by[repo]["last_indexed_at"] is None
