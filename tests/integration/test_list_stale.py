"""list_stale: ordering, paging and what a caller may see."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from knowledge_hub.mcp.errors import CURSOR_FOREIGN
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, dave_none, multi
from tests.support.seed import add_doc

pytestmark = pytest.mark.integration


@pytest.fixture
def app_settings(db_settings, tmp_path):
    from tests.support.mcp_client import HOST

    key = tmp_path / "cursor.key"
    key.write_bytes(b"k" * 32)
    return db_settings.model_copy(update={"env": "test", "allowed_hosts": HOST, "cursor_key_file": key})


@pytest.fixture
def stale_docs(seeded_db, fresh_db):
    """Five stale ``all`` documents (two share a date), one fresh, one stale ``mgmt``, one stale ``hr``."""
    out: list[str] = []
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        for day in (5, 1, 3, 3, 2):
            out.append(add_doc(conn, stale=True, review_by=date(2025, 1, day), title=f"stale {day}"))
        add_doc(conn, stale=False, review_by=date(2020, 1, 1), title="fresh")
        mg = add_doc(conn, repo="kb-mgmt", audience="mgmt", stale=True, review_by=date(2025, 2, 1))
        hr = add_doc(conn, repo="kb-hr", audience="hr", stale=True, review_by=date(2025, 3, 1))
    return {"all": out, "mgmt": mg, "hr": hr}


def _ids(res) -> list[str]:
    return [d["id"] for d in res.data["documents"]]


async def test_oldest_review_by_first_then_id(mcp_client, stale_docs):
    res = await mcp_client.call(alice_all, "list_stale")
    assert not res.is_error, res.text
    docs = res.data["documents"]
    keys = [(d["review_by"], d["id"]) for d in docs]
    assert keys == sorted(keys)
    assert [d["review_by"] for d in docs] == [f"2025-01-0{n}" for n in (1, 2, 3, 3, 5)]
    assert {d["id"] for d in docs} == {f"doc:{i}" for i in stale_docs["all"]}
    assert res.data["next_cursor"] == ""
    assert set(docs[0]) == {"id", "title", "audience", "labels", "repo", "path", "review_by", "stale"}


async def test_paging_covers_every_row_once(mcp_client, stale_docs):
    seen: list[str] = []
    cursor = ""
    for _ in range(5):
        res = await mcp_client.call(alice_all, "list_stale", {"limit": 2, "cursor": cursor})
        assert not res.is_error, res.text
        seen += _ids(res)
        cursor = res.data["next_cursor"]
        if not cursor:
            break
    full = _ids(await mcp_client.call(alice_all, "list_stale"))
    assert seen == full and len(seen) == 5


async def test_caller_filtering(mcp_client, stale_docs):
    bob = await mcp_client.call(bob_mgmt, "list_stale")
    assert _ids(bob) == [f"doc:{stale_docs['mgmt']}"]
    both = await mcp_client.call(multi, "list_stale")
    assert len(_ids(both)) == 6 and f"doc:{stale_docs['hr']}" not in _ids(both)
    none = await mcp_client.call(dave_none, "list_stale")
    assert none.data == {"documents": [], "next_cursor": ""}


async def test_audience_narrows(mcp_client, stale_docs):
    res = await mcp_client.call(multi, "list_stale", {"audience": "mgmt"})
    assert _ids(res) == [f"doc:{stale_docs['mgmt']}"]
    res = await mcp_client.call(alice_all, "list_stale", {"audience": "hr"})
    assert res.data["documents"] == []


async def test_a_cursor_is_bound_to_the_caller_and_the_filter(mcp_client, stale_docs):
    first = await mcp_client.call(multi, "list_stale", {"limit": 1})
    cursor = first.data["next_cursor"]
    assert cursor
    other = await mcp_client.call(alice_all, "list_stale", {"limit": 1, "cursor": cursor})
    assert other.is_error and other.text.endswith(CURSOR_FOREIGN)
    narrowed = await mcp_client.call(multi, "list_stale", {"limit": 1, "audience": "all", "cursor": cursor})
    assert narrowed.is_error and narrowed.text.endswith(CURSOR_FOREIGN)
    junk = await mcp_client.call(multi, "list_stale", {"cursor": "abc.def"})
    assert junk.is_error and junk.text.endswith(CURSOR_FOREIGN)


async def test_a_superseded_stale_document_is_not_listed(mcp_client, stale_docs, fresh_db):
    hidden, keep = stale_docs["all"][0], stale_docs["all"][1]
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        conn.execute("UPDATE kh.documents SET superseded_by = %s WHERE id = %s", (keep, hidden))
    res = await mcp_client.call(alice_all, "list_stale")
    assert not res.is_error, res.text
    assert f"doc:{hidden}" not in _ids(res)
    assert len(_ids(res)) == 4
