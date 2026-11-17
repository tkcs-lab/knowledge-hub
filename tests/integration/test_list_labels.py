"""list_labels: every label, ordered by name."""

from __future__ import annotations

import pytest

from tests.support.principals import alice_all, dave_none, wf_token

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("who", [alice_all, dave_none, wf_token], ids=lambda c: c.sub)
async def test_list_labels_is_ordered_by_name(mcp_client, seeded_db, who):
    res = await mcp_client.call(who, "list_labels")
    assert not res.is_error, res.text
    assert res.data == {
        "result": [
            {"name": "ember", "description": "Ember product"},
            {"name": "other", "description": "Another label"},
        ]
    }
