"""``kh daily`` (plan 3.12) against PostgreSQL with a fake Grist."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from knowledge_hub.config import Config
from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.jobs.daily import run_daily
from tests.integration.test_search_ranking import QUERY, add_doc, app_pool_of, deps, ids, search  # noqa: F401
from tests.support.principals import alice_all, bob_mgmt

TODAY = date(2026, 11, 15)
CONTRACTS = type(alice_all)(
    sub="carol", principal_type="claude_client", email="c@example.org",
    audiences=frozenset({"all", "contracts"}), token_id=None,
)


class FakeGrist:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    async def fetch_table(self, name: str) -> list[dict]:
        return self.tables.get(name, [])


def grist(*, cus: bool = True, extra=()) -> FakeGrist:
    rows = {
        "Products": [{"key": "prd:gateway", "name": "Gateway", "owner_team": "core", "status": "active",
                      "links": "https://x"}],
        "Components": [{"key": "cmp:auth", "name": "Auth"}],
        "Versions": [{"key": "ver:v1", "name": "v1"}],
        "Customers": [{"key": "cus:acme", "name": "Acme"}] if cus else [],
    }
    for table, row in extra:
        rows[table] = rows[table] + [row]
    return FakeGrist(rows)


async def entity_ids(pool, caller) -> list[str]:
    async with caller_tx(app_pool_of(pool), caller) as conn:
        cur = await conn.execute("SELECT id FROM kh.entities_cache ORDER BY id")
        return [r[0] for r in await cur.fetchall()]


async def set_review_by(pool, citation: str, review_by: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE kh.documents SET review_by = %s WHERE id = %s",
                           (review_by, uuid.UUID(citation.removeprefix("doc:"))))


async def test_stale_flag_and_ranking(deps, indexer_pool, mcp_client):  # noqa: F811
    old = await add_doc(indexer_pool, QUERY)
    fresh = await add_doc(indexer_pool, QUERY)
    await set_review_by(indexer_pool, old, "2026-11-14")
    await set_review_by(indexer_pool, fresh, "2026-11-15")
    res = await run_daily(indexer_pool, grist(), Config(), TODAY)
    assert res["stale_changed"] == 1
    out = await search(mcp_client, alice_all)
    assert ids(out) == [fresh, old]
    assert out.data["hits"][1]["stale"] is True and out.data["hits"][0]["stale"] is False


async def test_customers_invisible_without_contracts(deps, indexer_pool):  # noqa: F811
    await run_daily(indexer_pool, grist(), Config(), TODAY)
    assert await entity_ids(indexer_pool, alice_all) == ["cmp:auth", "prd:gateway", "ver:v1"]
    assert await entity_ids(indexer_pool, bob_mgmt) == ["cmp:auth", "prd:gateway", "ver:v1"]
    assert "cus:acme" in await entity_ids(indexer_pool, CONTRACTS)
    async with indexer_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT kind, name, owner_team, status, attrs, required_audiences, allowed_emails "
            "FROM kh.entities_cache WHERE id = 'prd:gateway'"
        )
        assert await cur.fetchone() == (
            "prd", "Gateway", "core", "active", {"links": "https://x"}, ["all"], None)


async def test_config_change_applies_at_next_run(deps, indexer_pool):  # noqa: F811
    await run_daily(indexer_pool, grist(), Config(), TODAY)
    cfg = Config(entities={"prd": ["contracts"], "cmp": ["all"], "ver": ["all"], "cus": ["contracts"]})
    await run_daily(indexer_pool, grist(), cfg, TODAY)
    assert await entity_ids(indexer_pool, alice_all) == ["cmp:auth", "ver:v1"]


async def test_empty_audience_config_refused(indexer_pool):
    cfg = Config(entities={"prd": [], "cmp": ["all"], "ver": ["all"], "cus": ["contracts"]})
    with pytest.raises(ValueError, match="no audience"):
        await run_daily(indexer_pool, grist(), cfg, TODAY)


async def test_removed_entity_disappears(deps, indexer_pool):  # noqa: F811
    await run_daily(indexer_pool, grist(), Config(), TODAY)
    res = await run_daily(indexer_pool, grist(cus=False), Config(), TODAY)
    assert res["entities_removed"] == 1
    assert "cus:acme" not in await entity_ids(indexer_pool, CONTRACTS)


async def test_rows_with_wrong_prefix_are_skipped(deps, indexer_pool):  # noqa: F811
    g = grist(extra=[("Products", {"key": "cus:oops", "name": "x"}), ("Products", {"name": "nokey"})])
    res = await run_daily(indexer_pool, g, Config(), TODAY)
    assert res["entities_skipped"] == 2
    assert "cus:oops" not in await entity_ids(indexer_pool, CONTRACTS)


async def test_expired_jti_purged(deps, indexer_pool, db_settings):  # noqa: F811
    import psycopg

    with psycopg.connect(f"{db_settings.dsn('migrator')}", autocommit=True) as admin:
        admin.execute("INSERT INTO kh.claim_jti (jti, expires_at) VALUES ('old', now() - interval '1 hour'),"
                      " ('live', now() + interval '1 hour')")
        res = await run_daily(indexer_pool, grist(), Config(), TODAY)
        assert res["jti_purged"] == 1
        left = [r[0] for r in admin.execute("SELECT jti FROM kh.claim_jti").fetchall()]
    assert left == ["live"]


async def test_rerun_changes_nothing(deps, indexer_pool):  # noqa: F811
    doc = await add_doc(indexer_pool, QUERY)
    await set_review_by(indexer_pool, doc, "2026-01-01")
    await run_daily(indexer_pool, grist(), Config(), TODAY)

    async def snapshot():
        async with indexer_pool.connection() as conn:
            e = await (await conn.execute("SELECT * FROM kh.entities_cache ORDER BY id")).fetchall()
            d = await (await conn.execute("SELECT id, stale, updated_at FROM kh.documents")).fetchall()
            return e, d

    before = await snapshot()
    res = await run_daily(indexer_pool, grist(), Config(), TODAY)
    assert await snapshot() == before
    assert res["stale_changed"] == 0 and res["entities_removed"] == 0
