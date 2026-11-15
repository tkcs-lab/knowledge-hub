"""get_entity and list_entities: visibility of cached entities and of the documents that list them."""

import psycopg
import pytest

from knowledge_hub.config import Config
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.server import Deps
from tests.integration.test_derived_visibility_sql import doc as make_doc
from tests.support.pg import FRESH_DB
from tests.support.principals import PRINCIPALS, alice_all, bob_mgmt, carol_hr
from tests.support.tool_cases import TOOL_CASES

BAD_TYPE = "type must be one of prd, cmp, ver, cus"


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


def entity(admin, eid, aud=("all",), status="active", team="core"):
    admin.execute(
        "INSERT INTO kh.entities_cache (id, kind, name, owner_team, status, attrs, required_audiences) "
        "VALUES (%s, %s, %s, %s, %s, '{}', %s)",
        (eid, eid.split(":")[0], f"name {eid}", team, status, list(aud)),
    )
    return eid


def listing(admin, entities, aud=("all",)):
    did = make_doc(admin, aud)
    admin.execute("UPDATE kh.documents SET entities = %s WHERE id = %s", (list(entities), did))
    return f"doc:{did}"


async def ok(client, who, tool, **args) -> dict:
    res = await client.call(who, tool, args)
    assert not res.is_error, res
    return res.data


def ids(page) -> list[str]:
    return [e["id"] for e in page["entities"]]


def test_tool_cases_registered():
    assert {"get_entity", "list_entities"} <= {t.name for t in TOOL_CASES}


async def test_customer_entity_is_not_found_without_contracts(admin, mcp_client):
    entity(admin, "cus:acme", aud=("contracts",))
    entity(admin, "prd:gateway")
    missing = await mcp_client.call(alice_all, "get_entity", {"id": "cus:nothing"})
    for name, who in PRINCIPALS.items():
        res = await mcp_client.call(who, "get_entity", {"id": "cus:acme"})
        if "contracts" in who.audiences:
            assert not res.is_error and res.data["entity"]["id"] == "cus:acme", name
        else:
            assert res.is_error and res.text.endswith(errors.NOT_FOUND), name
            assert (res.status, res.text, res.data) == (missing.status, missing.text, missing.data), name
        listed = await ok(mcp_client, who, "list_entities", type="cus")
        assert ids(listed) == (["cus:acme"] if "contracts" in who.audiences else []), name


async def test_get_entity_lists_only_visible_documents(admin, mcp_client):
    entity(admin, "prd:gateway")
    a = listing(admin, ["prd:gateway"])
    m = listing(admin, ["prd:gateway", "cmp:x"], ("mgmt",))
    h = listing(admin, ["prd:gateway"], ("hr",))
    listing(admin, ["prd:other"])
    want = {alice_all: {a}, bob_mgmt: {a, m}, carol_hr: {a, h}}
    for who, docs in want.items():
        data = await ok(mcp_client, who, "get_entity", id="prd:gateway")
        assert {d["citation_id"] for d in data["documents"]} == docs
        assert data["entity"]["name"] == "name prd:gateway" and data["entity"]["owner_team"] == "core"


async def test_list_entities_filters_and_bad_type(admin, mcp_client):
    entity(admin, "prd:a", team="x")
    entity(admin, "prd:b", status="retired", team="y")
    entity(admin, "cmp:c")
    assert ids(await ok(mcp_client, alice_all, "list_entities", type="prd")) == ["prd:a", "prd:b"]
    assert ids(await ok(mcp_client, alice_all, "list_entities", type="prd", status="retired")) == ["prd:b"]
    assert ids(await ok(mcp_client, alice_all, "list_entities", type="prd", owner_team="x")) == ["prd:a"]
    assert ids(await ok(mcp_client, alice_all, "list_entities", type="cmp")) == ["cmp:c"]
    for bad in ("", "doc", "PRD"):
        res = await mcp_client.call(alice_all, "list_entities", {"type": bad})
        assert res.is_error and res.text.endswith(BAD_TYPE), bad


async def test_pagination_is_stable_and_never_counts_hidden_rows(admin, mcp_client):
    for i in range(5):
        entity(admin, f"prd:v{i}")
    runs = []
    for _ in range(2):
        pages, cursor = [], ""
        for _ in range(3):
            page = await ok(mcp_client, alice_all, "list_entities", type="prd", limit=2, cursor=cursor)
            pages.append(page)
            cursor = page["next_cursor"]
        first, second, third = pages
        runs.append((first, second, third))
        for i in range(3):  # hidden rows sorting before, among and after the visible ones
            entity(admin, f"prd:a{i}{len(runs)}", aud=("hr",))
            entity(admin, f"prd:v{i}z{len(runs)}", aud=("mgmt",))
            entity(admin, f"prd:z{i}{len(runs)}", aud=("contracts",))
    assert runs[0] == runs[1]
    seen = [i for page in runs[0] for i in ids(page)]
    assert seen == [f"prd:v{i}" for i in range(5)]
    assert runs[0][2]["next_cursor"] == ""
    # A page that ends exactly on the last visible row has no cursor, hidden rows or not.
    exact = await ok(mcp_client, alice_all, "list_entities", type="prd", limit=5)
    assert exact["next_cursor"] == "" and len(exact["entities"]) == 5


async def test_cursors_are_bound(admin, mcp_client):
    for i in range(3):
        entity(admin, f"prd:p{i}")
    token = (await ok(mcp_client, alice_all, "list_entities", type="prd", limit=1))["next_cursor"]
    for who, extra in ((bob_mgmt, {}), (alice_all, {"status": "active"}), (alice_all, {"type": "cmp"})):
        args = {"type": "prd", "limit": 1, "cursor": token, **extra}
        res = await mcp_client.call(who, "list_entities", args)
        assert res.is_error and res.text.endswith(errors.CURSOR_FOREIGN), (who, extra, res)
    for bad in ("nope", "a.b"):
        res = await mcp_client.call(alice_all, "list_entities", {"type": "prd", "cursor": bad})
        assert res.is_error and res.text.endswith(errors.CURSOR_FOREIGN)
