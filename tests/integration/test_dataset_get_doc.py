"""get_doc for ``row:`` ids: fields, refs, mirror link and refusals (plan 3.7)."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from knowledge_hub.admin.move_cmd import do_move
from knowledge_hub.mcp.errors import BAD_GET_ID, BAD_ID, NOT_FOUND, KhError
from tests.support.datasets import make_dataset, migrator_dsn
from tests.support.principals import alice_all, bob_mgmt, multi

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def world(seeded_db, fresh_db):
    make_dataset(fresh_db)
    return fresh_db


def insert(world, table, key, audience, **cols):
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with psycopg.connect(migrator_dsn(world), autocommit=True) as conn:
        return str(conn.execute(
            f"INSERT INTO kh_data.{table} (_key, _audience, _source_updated_at, {names})"
            f" VALUES (%s, %s, now(), {marks}) RETURNING _id",
            (key, audience, *cols.values()),
        ).fetchone()[0])


async def test_row_with_a_visible_ref(mcp_client, world):
    acc = insert(world, "accounts", "acme", "all", name="Acme Inc")
    deal = insert(world, "deals", "d1", "all", name="Big deal", amount=5, secret_note="hush", account="acme")
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"row:deals:{deal}"})
    assert not res.is_error, res.text
    got = res.data
    assert got["id"] == f"row:deals:{deal}" and got["tier"] == "T3"
    assert (got["dataset"], got["audience"], got["key"], got["title"]) == ("deals", "all", "d1", "Big deal")
    assert got["outdated"] is False and got["outdated_reason"] == ""
    assert got["fields"]["secret_note"] == "hush" and got["fields"]["amount"] == 5
    assert got["refs"]["account"] == {"id": f"row:accounts:{acc}", "title": "acme"}
    assert got["record"].startswith("rec:")
    back = await mcp_client.call(alice_all, "get_doc", {"id": got["record"]})
    assert back.data["row"] == f"row:deals:{deal}"


async def test_ref_to_a_row_of_another_held_group_is_null(mcp_client, world):
    insert(world, "accounts", "acme", "mgmt", name="Acme Inc")
    deal = insert(world, "deals", "d1", "all", name="Deal", account="acme")
    res = await mcp_client.call(multi, "get_doc", {"id": f"row:deals:{deal}"})
    assert res.data["refs"]["account"] is None


async def test_row_of_another_group_is_not_found(mcp_client, world):
    deal = insert(world, "deals", "d1", "mgmt", name="Deal")
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"row:deals:{deal}"})
    assert res.is_error and res.text.endswith(NOT_FOUND)
    res = await mcp_client.call(bob_mgmt, "get_doc", {"id": f"row:deals:{deal}"})
    assert not res.is_error, res.text
    missing = await mcp_client.call(alice_all, "get_doc", {"id": f"row:deals:{uuid.uuid4()}"})
    assert missing.is_error and missing.text.endswith(NOT_FOUND)


@pytest.mark.parametrize("bad", ["row:deals:not-a-uuid", "row:", "row:deals", "row:Bad:" + str(uuid.uuid4()),
                                 "row:deals:" + str(uuid.uuid4()).upper()])
async def test_malformed_row_ids(mcp_client, bad):
    res = await mcp_client.call(alice_all, "get_doc", {"id": bad})
    assert res.is_error and res.text.endswith(BAD_GET_ID)


async def test_move_still_refuses_row_ids_with_bad_id(deps, world):
    deal = insert(world, "deals", "d1", "all", name="Deal")
    with pytest.raises(KhError) as exc:
        await do_move(deps, "admin@example.test", f"row:deals:{deal}", "all", "mgmt")
    assert exc.value.text == BAD_ID
