"""The panel's dataset catalog against a real database (plan 72, 3.11)."""

from __future__ import annotations

import pytest

from knowledge_hub.panel import stats
from tests.support.datasets import make_dataset

pytestmark = pytest.mark.integration


async def test_dataset_catalog_lists_deals_with_columns(seeded_db, fresh_db, indexer_pool):
    make_dataset(fresh_db)
    catalog = {d["name"]: d for d in await stats.dataset_catalog(indexer_pool)}
    deals = catalog["deals"]
    assert deals["title"] == "Deals"
    assert deals["audiences"] == ["all", "mgmt"]
    assert deals["title_column"] == "name"
    assert "rows_estimate" in deals
    assert deals["rows_estimate"] is None or deals["rows_estimate"] >= 0
    names = [c["name"] for c in deals["columns"]]
    assert names == ["name", "stage", "amount", "close_date", "secret_note", "account"]
    stage = deals["columns"][1]
    assert stage["type"] == "choice" and stage["choices"] == ["open", "won", "lost"]
    assert stage["filterable"] is True and deals["columns"][4]["sensitive"] is True
    assert deals["columns"][5]["ref_dataset"] == "accounts"
    assert "accounts" in catalog
