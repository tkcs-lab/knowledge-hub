"""list_stale: visible stale or overdue documents, oldest review date first, keyset paging."""

import datetime

import psycopg
import pytest

from knowledge_hub.config import Config
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.server import Deps
from tests.integration.test_derived_visibility_sql import doc as make_doc
from tests.support.pg import FRESH_DB
from tests.support.principals import PRINCIPALS, alice_all, bob_mgmt, carol_hr

TODAY = datetime.date.today()


def days(n: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=n)


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


def stale_doc(admin, aud=("all",), review_by=None, stale=False, domain="product", owner="team"):
    did = make_doc(admin, aud)
    admin.execute(
        "UPDATE kh.documents SET review_by = %s, stale = %s, domain = %s, owner = %s WHERE id = %s",
        (review_by, stale, domain, owner, did),
    )
    return f"doc:{did}"


async def stale(client, who, **args) -> dict:
    res = await client.call(who, "list_stale", args)
    assert not res.is_error, res
    return res.data


def cites(page) -> list[str]:
    return [d["citation_id"] for d in page["documents"]]


async def test_only_stale_or_overdue_documents_oldest_first(admin, mcp_client):
    old = stale_doc(admin, review_by=days(-30))
    older = stale_doc(admin, review_by=days(-90))
    flagged = stale_doc(admin, review_by=days(10), stale=True)
    nodate = stale_doc(admin, stale=True)
    stale_doc(admin, review_by=days(5))  # not due
    stale_doc(admin)  # no review date, not stale
    page = await stale(mcp_client, alice_all)
    assert cites(page) == [older, old, flagged, nodate]
    assert page["next_cursor"] == ""
    assert page["documents"][0]["review_by"] == days(-90).isoformat()


async def test_each_principal_sees_only_visible_documents(admin, mcp_client):
    a = stale_doc(admin, ("all",), days(-1))
    m = stale_doc(admin, ("mgmt",), days(-2))
    h = stale_doc(admin, ("hr",), days(-3))
    want = {
        "alice_all": {a},
        "bob_mgmt": {a, m},
        "carol_hr": {a, h},
        "dave_none": set(),
        "eve_nobody": set(),
        "wf_token": {a},
    }
    for name, who in PRINCIPALS.items():
        page = await stale(mcp_client, who)
        assert set(cites(page)) == want[name], name
        assert set(page) == {"documents", "next_cursor"}


async def test_filters(admin, mcp_client):
    a = stale_doc(admin, review_by=days(-1), domain="product", owner="x")
    b = stale_doc(admin, review_by=days(-2), domain="technical", owner="y")
    assert cites(await stale(mcp_client, alice_all, domain="technical")) == [b]
    assert cites(await stale(mcp_client, alice_all, owner="x")) == [a]
    assert cites(await stale(mcp_client, alice_all, domain="technical", owner="x")) == []


async def test_paging_and_foreign_cursors(admin, mcp_client):
    ids = [stale_doc(admin, review_by=days(-10 + i)) for i in range(4)]
    ids.append(stale_doc(admin, stale=True))  # null review date sorts last
    seen, cursor = [], ""
    while True:
        page = await stale(mcp_client, alice_all, limit=2, cursor=cursor)
        seen += cites(page)
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == ids
    token = (await stale(mcp_client, alice_all, limit=2))["next_cursor"]
    for who, extra in ((bob_mgmt, {}), (alice_all, {"owner": "other"}), (alice_all, {"domain": "technical"})):
        res = await mcp_client.call(who, "list_stale", {"limit": 2, "cursor": token, **extra})
        assert res.is_error and res.text.endswith(errors.CURSOR_FOREIGN), res
    bad = await mcp_client.call(alice_all, "list_stale", {"cursor": "nope"})
    assert bad.is_error and bad.text.endswith(errors.CURSOR_FOREIGN)


async def test_pages_never_change_with_hidden_rows(admin, mcp_client):
    for i in range(3):
        stale_doc(admin, review_by=days(-20 + i))
    runs = []
    for _ in range(2):
        first = await stale(mcp_client, alice_all, limit=2)
        second = await stale(mcp_client, alice_all, limit=2, cursor=first["next_cursor"])
        runs.append((first, second))
        for i in range(4):  # hidden rows sorting before, among and after the visible ones
            stale_doc(admin, ("hr",), days(-100 + 30 * i))
            stale_doc(admin, ("mgmt",), stale=True)
    assert runs[0] == runs[1]
    hr_view = await stale(mcp_client, carol_hr)
    assert len(hr_view["documents"]) == 3 + 8
