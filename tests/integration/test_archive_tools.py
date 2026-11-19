"""Archived groups and labels in ingest_record, propose_doc, list_labels, search, get_doc, list_stale."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from knowledge_hub.config import GroupConfig
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.forgejo import ForgejoBot
from knowledge_hub.mcp.errors import AUDIENCE_NOT_HELD, BAD_LABEL
from knowledge_hub.mcp.server import Deps
from tests.support.forgejo_fake import FakeForgejo
from tests.support.mcp_client import HOST
from tests.support.pg import FRESH_DB
from tests.support.principals import KH_CONFIG, alice_all, carol_hr
from tests.support.seed import add_chunked_doc

pytestmark = pytest.mark.integration


@pytest.fixture
def kh_config():
    return KH_CONFIG.model_copy(
        update={
            "labels": {"ember": "Ember product", "other": "Another label"},
            "archived_labels": ["other"],
            "groups": {
                **KH_CONFIG.groups,
                "hr": GroupConfig(repo="kb-hr", members=["carol"], approvers=["carol"], archived=True),
            },
        }
    )


@pytest.fixture
def app_settings(db_settings, tmp_path):
    key = tmp_path / "cursor.key"
    key.write_bytes(b"k" * 32)
    return db_settings.model_copy(update={"env": "test", "allowed_hosts": HOST, "cursor_key_file": key})


@pytest.fixture
def forgejo() -> FakeForgejo:
    fake = FakeForgejo()
    fake.add_repo("kb-all")
    fake.add_repo("kb-hr")
    return fake


@pytest.fixture
def deps(app_settings, kh_config, app_pool, indexer_pool, forgejo) -> Deps:
    out = Deps(
        settings=app_settings,
        config=kh_config,
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        embedder=FakeEmbedder(),
    )
    out.forgejo_bot = ForgejoBot("https://forgejo.test", "tok", "kh", transport=forgejo.transport)
    return out


def _insert(fresh_db, **fields) -> str:
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        doc = add_chunked_doc(
            conn,
            "archive probe text",
            embed=False,
            labels=["ember", "other"],
            audience="all",
            repo="kb-all",
            **fields,
        )
        conn.commit()
    return doc


RECORD = {
    "source": "meet",
    "source_ref": "r1",
    "title": "T",
    "body": "B",
    "occurred_at": "2026-01-02T03:04:05+00:00",
}
PROPOSE = {
    "path": "guides/new.md",
    "id": "new-guide",
    "title": "A new guide",
    "review_by": "2027-06-30",
    "body": "# Heading\n\nSome text.\n",
}


async def test_archived_label_is_not_listed(mcp_client, seeded_db):
    res = await mcp_client.call(alice_all, "list_labels")
    assert not res.is_error, res.text
    assert res.data == {"result": [{"name": "ember", "description": "Ember product"}]}


async def test_ingest_with_archived_label_is_unknown_label(mcp_client, seeded_db):
    res = await mcp_client.call(
        alice_all, "ingest_record", {**RECORD, "audience": "all", "labels": ["other"]}
    )
    assert res.is_error and BAD_LABEL in res.text and "unknown label" in res.text


async def test_ingest_into_archived_group_is_refused(mcp_client, seeded_db):
    res = await mcp_client.call(carol_hr, "ingest_record", {**RECORD, "audience": "hr"})
    assert res.is_error and AUDIENCE_NOT_HELD in res.text


async def test_propose_with_archived_label_is_unknown_label(mcp_client, seeded_db):
    res = await mcp_client.call(alice_all, "propose_doc", {**PROPOSE, "group": "all", "labels": ["other"]})
    assert res.is_error and "unknown label" in res.text


async def test_propose_into_archived_group_is_refused(mcp_client, seeded_db):
    res = await mcp_client.call(carol_hr, "propose_doc", {**PROPOSE, "group": "hr"})
    assert res.is_error and AUDIENCE_NOT_HELD in res.text


async def test_search_filtered_by_archived_label_finds_nothing(mcp_client, seeded_db, fresh_db):
    _insert(fresh_db)
    res = await mcp_client.call(alice_all, "search", {"query": "archive probe", "labels": ["other"]})
    assert not res.is_error, res.text
    assert res.data["hits"] == []
    res = await mcp_client.call(alice_all, "search", {"query": "archive probe", "labels": ["ember"]})
    assert not res.is_error, res.text
    assert [h["labels"] for h in res.data["hits"]] == [["ember"]]


async def test_get_doc_hides_archived_label(mcp_client, seeded_db, fresh_db):
    doc = _insert(fresh_db)
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{doc}"})
    assert not res.is_error, res.text
    assert res.data["labels"] == ["ember"]


async def test_list_stale_hides_archived_label(mcp_client, seeded_db, fresh_db):
    doc = _insert(fresh_db, review_by=date(2020, 1, 1), stale=True)
    res = await mcp_client.call(alice_all, "list_stale")
    assert not res.is_error, res.text
    (found,) = [d for d in res.data["documents"] if d["id"] == f"doc:{doc}"]
    assert found["labels"] == ["ember"]
