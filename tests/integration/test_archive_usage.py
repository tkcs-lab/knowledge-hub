"""An archived label never appears in the labels the usage tool returns (plan 3.4)."""

from __future__ import annotations

import psycopg
import pytest

from tests.support.pg import FRESH_DB
from tests.support.principals import KH_CONFIG, alice_all
from tests.support.seed import add_record, add_usage

pytestmark = pytest.mark.integration


@pytest.fixture
def kh_config():
    return KH_CONFIG.model_copy(
        update={
            "labels": {"ember": "Ember product", "other": "Another label"},
            "archived_labels": ["other"],
        }
    )


@pytest.fixture
def db(fresh_db, seeded_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


async def test_usage_records_hide_archived_labels(mcp_client, db):
    rec = add_record(db, title="labelled", labels=["ember", "other"])
    for n in (1, 2):
        add_usage(db, who=f"{n:032x}", owner_keys=[f"rec:{rec}"])
    res = await mcp_client.call(alice_all, "usage", {"view": "records"})
    assert not res.is_error, res.text
    [item] = res.data["items"]
    assert item["labels"] == ["ember"]
