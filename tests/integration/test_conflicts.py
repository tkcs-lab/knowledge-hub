"""flag_conflict and list_conflicts: strict resolution, visibility and paging (plan 3.4, 3.8)."""

import uuid

import psycopg
import pytest

from knowledge_hub.config import Config
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.server import Deps
from tests.integration.test_derived_visibility_sql import caller
from tests.integration.test_derived_visibility_sql import doc as make_doc
from tests.integration.test_derived_visibility_sql import raw as make_raw
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, carol_hr, dave_none

BOTH = caller(("all", "mgmt", "hr"), sub="both")


@pytest.fixture
def app_settings(app_settings, tmp_path):
    key = tmp_path / "cursor.key"
    key.write_bytes(b"k" * 32)
    return app_settings.model_copy(update={"cursor_key_file": key})


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool):
    return Deps(settings=app_settings, config=Config(), app_pool=app_pool, indexer_pool=indexer_pool)


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


def ok(res) -> dict:
    assert not res.is_error, res
    return res.data


def err(res, text) -> None:
    assert res.status == 200 and res.is_error and res.text.endswith(text), res


async def flag(client, who, a, b, note="they disagree"):
    return await client.call(who, "flag_conflict", {"a": a, "b": b, "note": note})


async def listed(client, who, **args) -> dict:
    return ok(await client.call(who, "list_conflicts", args))


def n_conflicts(admin) -> int:
    return admin.execute("SELECT count(*) FROM kh.conflicts").fetchone()[0]


# --- visibility --------------------------------------------------------------------------------


async def test_conflict_between_mgmt_and_hr_is_visible_only_to_holders_of_both(admin, mcp_client):
    m, h = make_doc(admin, ("mgmt",)), make_doc(admin, ("hr",))
    cid = ok(await flag(mcp_client, BOTH, f"doc:{m}", f"doc:{h}"))["conflict_id"]
    row = admin.execute(
        "SELECT required_audiences, allowed_emails, created_by_sub, state, domain, note"
        " FROM kh.conflicts WHERE id = %s",
        (cid,),
    ).fetchone()
    assert sorted(row[0]) == ["hr", "mgmt"] and row[1] is None
    assert row[2:] == ("both", "open", "product", "they disagree")
    items = admin.execute(
        "SELECT item_kind, item_id, src_required_audiences FROM kh.conflict_items"
        " WHERE conflict_id = %s ORDER BY item_id",
        (cid,),
    ).fetchall()
    assert {(k, i) for k, i, _ in items} == {("doc", m), ("doc", h)}
    for who in (alice_all, bob_mgmt, carol_hr, dave_none):
        assert (await listed(mcp_client, who))["conflicts"] == []
    page = await listed(mcp_client, BOTH)
    assert [c["id"] for c in page["conflicts"]] == [cid]
    assert sorted(page["conflicts"][0]["items"]) == sorted([f"doc:{m}", f"doc:{h}"])


async def test_caller_cannot_flag_an_item_it_cannot_see(admin, mcp_client):
    visible, hidden = make_doc(admin, ("all",)), make_doc(admin, ("hr",))
    missing = await flag(mcp_client, bob_mgmt, f"doc:{visible}", f"doc:{uuid.uuid4()}")
    err(missing, errors.NOT_FOUND)
    for a, b in ((visible, hidden), (hidden, visible)):
        res = await flag(mcp_client, bob_mgmt, f"doc:{a}", f"doc:{b}")
        assert res == missing
    assert n_conflicts(admin) == 0


async def test_raw_and_derived_items_and_attendee_only_visibility(admin, mcp_client):
    d = make_doc(admin, ("all",))
    r = make_raw(admin, aud=(), emails=["dave@example.org"])
    # dave sees the attendee-only raw record and the {all}-less caller cannot see the {all} doc.
    err(await flag(mcp_client, dave_none, f"doc:{d}", f"raw:{r}"), errors.NOT_FOUND)
    ok(await flag(mcp_client, alice_all, f"doc:{d}", f"doc:{make_doc(admin, ('all',))}"))
    err(await flag(mcp_client, alice_all, f"doc:{d}", f"raw:{r}"), errors.NOT_FOUND)


async def test_bad_arguments(admin, mcp_client):
    a, b = make_doc(admin, ("all",)), make_doc(admin, ("all",))
    err(await flag(mcp_client, alice_all, f"doc:{a}", f"doc:{a}"), "a and b must name two different records")
    err(await flag(mcp_client, alice_all, f"doc:{a}", "nonsense"), errors.NOT_FOUND)
    err(
        await flag(mcp_client, alice_all, f"doc:{a}", f"doc:{b}", note=""),
        "note must be 1 to 2000 characters",
    )
    err(
        await flag(mcp_client, alice_all, f"doc:{a}", f"doc:{b}", note="x" * 2001),
        "note must be 1 to 2000 characters",
    )
    assert n_conflicts(admin) == 0
    ok(await flag(mcp_client, alice_all, f"doc:{a}", f"doc:{b}", note="x" * 2000))
    assert n_conflicts(admin) == 1


# --- list_conflicts ----------------------------------------------------------------------------


async def test_list_filters_by_domain_and_status(admin, mcp_client):
    ids = []
    for _ in range(3):
        ids.append(
            ok(await flag(mcp_client, alice_all, f"doc:{make_doc(admin)}", f"doc:{make_doc(admin)}"))[
                "conflict_id"
            ]
        )
    admin.execute("UPDATE kh.conflicts SET domain = 'portfolio' WHERE id = %s", (ids[0],))
    admin.execute("UPDATE kh.conflicts SET state = 'closed', closed_at = now() WHERE id = %s", (ids[1],))
    got = lambda page: {c["id"] for c in page["conflicts"]}  # noqa: E731
    assert got(await listed(mcp_client, alice_all)) == set(ids)
    assert got(await listed(mcp_client, alice_all, domain="portfolio")) == {ids[0]}
    assert got(await listed(mcp_client, alice_all, domain="product")) == set(ids[1:])
    assert got(await listed(mcp_client, alice_all, status="closed")) == {ids[1]}
    assert got(await listed(mcp_client, alice_all, status="open")) == {ids[0], ids[2]}
    assert got(await listed(mcp_client, alice_all, domain="product", status="open")) == {ids[2]}
    assert got(await listed(mcp_client, alice_all, domain="technical")) == set()


async def test_keyset_paging_and_foreign_cursors(admin, mcp_client):
    ids = [
        ok(await flag(mcp_client, alice_all, f"doc:{make_doc(admin)}", f"doc:{make_doc(admin)}"))[
            "conflict_id"
        ]
        for _ in range(5)
    ]
    seen, cursor = [], ""
    while True:
        page = await listed(mcp_client, alice_all, limit=2, cursor=cursor)
        seen += [c["id"] for c in page["conflicts"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
        assert len(page["conflicts"]) == 2
    assert sorted(seen) == sorted(ids) and len(set(seen)) == 5
    assert seen == list(reversed(ids))  # newest first

    first = await listed(mcp_client, alice_all, limit=2)
    token = first["next_cursor"]
    foreign = errors.CURSOR_FOREIGN
    err(await mcp_client.call(bob_mgmt, "list_conflicts", {"limit": 2, "cursor": token}), foreign)
    err(
        await mcp_client.call(alice_all, "list_conflicts", {"limit": 2, "cursor": token, "status": "open"}),
        foreign,
    )
    err(
        await mcp_client.call(alice_all, "list_conflicts", {"limit": 2, "cursor": token[:-2] + "AA"}), foreign
    )
    err(await mcp_client.call(alice_all, "list_conflicts", {"cursor": "garbage"}), foreign)
    ok(await mcp_client.call(alice_all, "list_conflicts", {"limit": 2, "cursor": token}))


async def test_pages_never_change_with_hidden_conflicts(admin, mcp_client):
    for _ in range(3):
        ok(await flag(mcp_client, alice_all, f"doc:{make_doc(admin)}", f"doc:{make_doc(admin)}"))
    before = [await listed(mcp_client, alice_all, limit=2)]
    before.append(await listed(mcp_client, alice_all, limit=2, cursor=before[0]["next_cursor"]))
    for _ in range(3):
        ok(
            await flag(
                mcp_client, carol_hr, f"doc:{make_doc(admin, ('hr',))}", f"doc:{make_doc(admin, ('hr',))}"
            )
        )
    after = [await listed(mcp_client, alice_all, limit=2)]
    after.append(await listed(mcp_client, alice_all, limit=2, cursor=after[0]["next_cursor"]))
    assert after == before


async def test_a_flag_writes_one_conflict_with_two_items(admin, mcp_client):
    ok(await flag(mcp_client, alice_all, f"doc:{make_doc(admin)}", f"doc:{make_doc(admin)}"))
    assert n_conflicts(admin) == 1
    assert admin.execute("SELECT count(*) FROM kh.conflict_items").fetchone()[0] == 2
