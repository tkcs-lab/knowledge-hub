"""Every registered tool call, for every principal and seed case, shows only what that principal may see.

Hidden rows must be indistinguishable from rows that do not exist (plan 3.3, 3.9).
"""

import json

import pytest

from tests.integration.test_search_ranking import app_settings, deps  # noqa: F401  (fixtures)
from tests.support.principals import PRINCIPALS
from tests.support.seed import CASES
from tests.support.tool_cases import TOOL_CASES

TABLE_PREFIX = {"documents": "doc", "raw_records": "raw", "derived_records": "derived"}


def visible(caller, case: str) -> bool:
    audiences, emails = CASES[case]
    return set(audiences) <= caller.audiences and (emails is None or caller.email in emails)


def fingerprint(res) -> bytes:
    """Everything a caller can observe of a response."""
    return json.dumps([res.status, res.is_error, res.text, res.data], sort_keys=True).encode()


def row_ids(seeded_db, cases) -> set[str]:
    return {str(seeded_db[t][c]) for t in TABLE_PREFIX for c in cases}


@pytest.mark.parametrize("principal", sorted(PRINCIPALS))
@pytest.mark.parametrize("tool", TOOL_CASES, ids=lambda t: t.id)
async def test_response_holds_nothing_outside_the_principal_visibility(
    seeded_db, mcp_client, tool, principal
):
    caller = PRINCIPALS[principal]
    missing = await mcp_client.call(caller, tool.name, tool.args_for_missing(seeded_db))
    for case in tool.cases:
        if visible(caller, case):
            res = await mcp_client.call(caller, tool.name, tool.args_for_visible(seeded_db, case))
            assert not res.is_error, (case, res)
            assert tool.citation(seeded_db, case) in tool.cited(res), (case, res)
        else:
            res = await mcp_client.call(caller, tool.name, tool.args_for_hidden(seeded_db, case))
            tool.expect_hidden(res)
            assert fingerprint(res) == fingerprint(missing), (case, res, missing)
        # Nothing the principal may not see appears, whatever was asked for.
        hidden_citations = {
            f"{p}:{seeded_db[t][c]}" for t, p in TABLE_PREFIX.items() for c in CASES if not visible(caller, c)
        }
        assert tool.cited(res) & hidden_citations == set(), (case, res)
        body = json.dumps([res.text, res.data])
        hidden_cases = [c for c in CASES if not visible(caller, c)]
        for hidden_id in row_ids(seeded_db, hidden_cases):
            assert hidden_id not in body, (case, hidden_id)

