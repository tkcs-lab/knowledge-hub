"""delete_records, the restore inside ingest_record and the purging refusal (plan 3.3)."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.server import Deps
from tests.support.bearer import TestBearerResolver
from tests.support.blobs import MemoryBlobStore
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, wf_all, wf_mgmt
from tests.support.seed import add_chunked_record, add_record

pytestmark = pytest.mark.integration

SRC = "drive"
INGEST = {
    "source": SRC,
    "source_ref": "f-1",
    "title": "T",
    "body": "body text",
    "audience": "all",
    "occurred_at": "2026-03-04T10:00:00+01:00",
}


@pytest.fixture
def cursor_key(tmp_path):
    path = tmp_path / "cursor.key"
    path.write_bytes(b"k" * 32)
    return path


@pytest.fixture
def app_settings(db_settings, cursor_key):
    from tests.support.mcp_client import HOST

    update = {"env": "test", "allowed_hosts": HOST, "cursor_key_file": cursor_key}
    return db_settings.model_copy(update=update)


@pytest.fixture
def deps(app_settings, kh_config, app_pool, indexer_pool) -> Deps:
    return Deps(
        settings=app_settings, config=kh_config, app_pool=app_pool, indexer_pool=indexer_pool,
        embedder=FakeEmbedder(), bearer=TestBearerResolver(kh_config), blobs=MemoryBlobStore(),
    )


@pytest.fixture
def db(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        yield conn
        conn.commit()


async def delete(mcp_client, caller=wf_all, **args):
    body = {"audience": "all", "source": SRC, **args}
    return await mcp_client.call(caller, "delete_records", body)


def state(db, rec: str):
    db.commit()
    return db.execute(
        "SELECT outdated, outdated_reason, outdated_note FROM kh.records WHERE id = %s", (rec,)
    ).fetchone()


async def test_delete_records_hides_exact_and_prefix_matches(mcp_client, db):
    a = add_record(db, source=SRC, source_ref="doc_1")
    b = add_record(db, source=SRC, source_ref="dir/x")
    c = add_record(db, source=SRC, source_ref="dir/y")
    other = add_record(db, source=SRC, source_ref="keep")
    other_src = add_record(db, source="other", source_ref="doc_1")
    db.commit()
    res = await delete(mcp_client, source_refs=["doc_1"])
    assert not res.is_error, res.text
    assert res.data == {"outdated": 1}
    res = await delete(mcp_client, source_refs=["nomatch_x"], prefix=True)
    assert res.data == {"outdated": 0}
    assert state(db, a) == (True, "delete", "removed from source")
    assert state(db, b)[0] is False and state(db, other)[0] is False and state(db, other_src)[0] is False
    d1 = add_record(db, source=SRC, source_ref="folder_aa/1")
    d2 = add_record(db, source=SRC, source_ref="folder_aa/2")
    db.commit()
    res = await delete(mcp_client, source_refs=["folder_aa"], prefix=True)
    assert res.data == {"outdated": 2}
    assert state(db, d1)[1] == "delete" and state(db, d2)[1] == "delete"
    assert state(db, c)[0] is False


async def test_delete_records_counts_only_changed_rows(mcp_client, db):
    rec = add_record(db, source=SRC, source_ref="g1")
    db.commit()
    assert (await delete(mcp_client, source_refs=["g1"])).data == {"outdated": 1}
    assert (await delete(mcp_client, source_refs=["g1"])).data == {"outdated": 0}
    assert state(db, rec)[1] == "delete"


async def test_delete_records_refusals(mcp_client, db):
    res = await delete(mcp_client, alice_all, source_refs=["a"])
    assert res.is_error and res.text.endswith((errors.TOOL_NOT_ALLOWED, errors.ONLY_WORKFLOWS))
    res = await delete(mcp_client, wf_mgmt, source_refs=["a"])
    assert res.is_error and res.text.endswith(errors.AUDIENCE_NOT_HELD)
    res = await delete(mcp_client, source="dataset:x", source_refs=["a"])
    assert res.is_error and res.text.endswith(errors.BAD_ARG.format(name="source"))
    res = await delete(mcp_client, source_refs=[])
    assert res.is_error and res.text.endswith(errors.BAD_ARG.format(name="source_refs"))
    res = await delete(mcp_client, source_refs=["abcdefg"], prefix=True)
    assert res.is_error and res.text.endswith(errors.BAD_ARG.format(name="source_refs"))
    res = await delete(mcp_client, source_refs=["a\x00b"])
    assert res.is_error and res.text.endswith(errors.BAD_ARG.format(name="source_refs"))
    res = await delete(mcp_client, source_refs=[f"r{i}" for i in range(501)])
    assert res.is_error and res.text.endswith(errors.TOO_MANY_ROWS)


async def test_delete_records_cap_and_shrink_texts(mcp_client, db):
    for i in range(501):
        add_record(db, source=SRC, source_ref=f"big@xxxx{i}")
    db.commit()
    res = await delete(mcp_client, source_refs=["big@xxxx"], prefix=True, force=True)
    assert res.is_error and res.text.endswith(errors.RECORDS_CAP)
    res = await delete(mcp_client, source_refs=[f"big@xxxx{i}" for i in range(101)])
    assert res.is_error
    assert res.text.endswith(errors.RECORDS_SHRINK.format(n=101, limit=100))
    res = await delete(mcp_client, source_refs=[f"big@xxxx{i}" for i in range(101)], force=True)
    assert not res.is_error, res.text
    assert res.data == {"outdated": 101}


async def test_removed_record_is_hidden_from_search_and_readable_in_get_doc(mcp_client, db):
    rec = add_chunked_record(db, "wombat", source=SRC, source_ref="w1")
    db.commit()
    assert (await delete(mcp_client, source_refs=["w1"])).data == {"outdated": 1}
    found = await mcp_client.call(alice_all, "search", {"query": "wombat"})
    assert f"rec:{rec}" not in [h["id"] for h in found.data["hits"]]
    found = await mcp_client.call(alice_all, "search", {"query": "wombat", "include_outdated": True})
    assert f"rec:{rec}" in [h["id"] for h in found.data["hits"]]
    got = await mcp_client.call(alice_all, "get_doc", {"id": f"rec:{rec}"})
    assert got.data["outdated"] is True and got.data["note"] == "removed from source"


async def test_ingest_restores_a_removed_record(mcp_client, db):
    rec = add_record(db, source=SRC, source_ref="f-1")
    db.commit()
    await delete(mcp_client, source_refs=["f-1"])
    res = await mcp_client.call(wf_all, "ingest_record", INGEST)
    assert not res.is_error, res.text
    assert res.data["existed"] is True and res.data["restored"] is True
    assert res.data["id"] == f"rec:{rec}"
    assert state(db, rec) == (False, "", "")


async def test_ingest_refuses_a_purging_record(mcp_client, db):
    add_record(db, source=SRC, source_ref="f-1", outdated=True, outdated_at=datetime(2026, 1, 1, tzinfo=UTC),
               outdated_by="x", outdated_reason="purge")
    db.commit()
    res = await mcp_client.call(wf_all, "ingest_record", INGEST)
    assert res.is_error and res.text.endswith(errors.RECORD_PURGING)


async def test_ingest_leaves_a_manual_mark_alone(mcp_client, db):
    rec = add_record(db, source=SRC, source_ref="f-1", outdated=True,
                     outdated_at=datetime(2026, 1, 1, tzinfo=UTC), outdated_by="alice")
    db.commit()
    res = await mcp_client.call(wf_all, "ingest_record", INGEST)
    assert not res.is_error, res.text
    assert res.data["existed"] is True and "restored" not in res.data
    assert state(db, rec)[0] is True


async def test_mark_record_outdated_refuses_a_purging_record(mcp_client, db):
    rec = add_record(db, source=SRC, source_ref="p1", outdated=True,
                     outdated_at=datetime(2026, 1, 1, tzinfo=UTC), outdated_by="x", outdated_reason="purge")
    db.commit()
    res = await mcp_client.call(alice_all, "mark_record_outdated", {"id": f"rec:{rec}", "outdated": False})
    assert res.is_error and res.text.endswith(errors.RECORD_PURGING)
