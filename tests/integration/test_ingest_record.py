"""ingest_record: dedupe, refusals, S3 ordering, the embed job and audience isolation."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from knowledge_hub.mcp.errors import AUDIENCE_NOT_HELD, BAD_ARG, BAD_LABEL, TOO_LARGE
from tests.support.blobs import MemoryBlobStore
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, dave_none, wf_token

pytestmark = pytest.mark.integration

ARGS = {
    "source": "meet",
    "source_ref": "m-1",
    "title": "Weekly",
    "body": "We agreed on the plan.",
    "audience": "all",
    "occurred_at": "2026-03-04T10:00:00+01:00",
}


class FailingBlobStore(MemoryBlobStore):
    async def put(self, key: str, data: bytes, content_type: str) -> None:
        raise OSError("s3 down")


@pytest.fixture
def blobs() -> MemoryBlobStore:
    return MemoryBlobStore()


@pytest.fixture
def deps(deps, blobs):
    deps.blobs = blobs
    return deps


def _rows(fresh_db, sql: str, *params):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        return conn.execute(sql, params).fetchall()


async def test_new_record(mcp_client, seeded_db, fresh_db, blobs):
    res = await mcp_client.call(
        alice_all, "ingest_record", {**ARGS, "labels": ["ember"], "source_url": "https://x.test/m"}
    )
    assert not res.is_error, res.text
    assert res.data["existed"] is False
    rec_id = res.data["id"]
    assert rec_id.startswith("rec:")
    key = rec_id.removeprefix("rec:")
    assert blobs.objects[f"records/{key}"] == (b"We agreed on the plan.", "text/plain")
    (row,) = _rows(
        fresh_db,
        "SELECT audience, source, source_ref, source_url, title, blob_key, excerpt, content_type,"
        " occurred_at, labels, written_by, created_by_sub, created_by_type FROM kh.records WHERE id = %s",
        key,
    )
    assert row[:8] == ("all", "meet", "m-1", "https://x.test/m", "Weekly", f"records/{key}",
                       "We agreed on the plan.", "text/plain")
    assert row[8].utcoffset() is not None and row[8].isoformat().startswith("2026-03-04T09:00:00")
    assert row[9:] == (["ember"], "kh-bot", "alice", "claude_client")
    got = await mcp_client.call(alice_all, "get_doc", {"id": rec_id})
    assert got.data["body"] == "We agreed on the plan."


async def test_excerpt_is_the_first_2000_characters(mcp_client, seeded_db, fresh_db):
    res = await mcp_client.call(alice_all, "ingest_record", {**ARGS, "body": "é" * 2500})
    assert not res.is_error, res.text
    (row,) = _rows(fresh_db, "SELECT excerpt FROM kh.records WHERE id = %s", res.data["id"][4:])
    assert row[0] == "é" * 2000


async def test_duplicate_returns_the_existing_id(mcp_client, seeded_db, fresh_db, blobs):
    first = await mcp_client.call(alice_all, "ingest_record", ARGS)
    again = await mcp_client.call(alice_all, "ingest_record", {**ARGS, "title": "Other", "body": "x"})
    assert not again.is_error, again.text
    assert again.data == {"id": first.data["id"], "existed": True}
    assert len(blobs.objects) == 1
    assert _rows(fresh_db, "SELECT count(*) FROM kh.records WHERE source_ref = 'm-1'") == [(1,)]


async def test_concurrent_duplicate(mcp_client, seeded_db, fresh_db, blobs):
    results = await asyncio.gather(*(mcp_client.call(alice_all, "ingest_record", ARGS) for _ in range(4)))
    assert all(not r.is_error for r in results), [r.text for r in results]
    assert len({r.data["id"] for r in results}) == 1
    assert sorted(r.data["existed"] for r in results) == [False, True, True, True]
    assert _rows(fresh_db, "SELECT count(*) FROM kh.records WHERE source_ref = 'm-1'") == [(1,)]
    assert len(blobs.objects) == 1


async def test_audience_not_held(mcp_client, seeded_db, fresh_db, blobs):
    res = await mcp_client.call(alice_all, "ingest_record", {**ARGS, "audience": "hr"})
    assert res.is_error and res.text.endswith(AUDIENCE_NOT_HELD)
    res = await mcp_client.call(dave_none, "ingest_record", ARGS)
    assert res.is_error and res.text.endswith(AUDIENCE_NOT_HELD)
    assert not blobs.objects
    assert _rows(fresh_db, "SELECT count(*) FROM kh.records WHERE source_ref = 'm-1'") == [(0,)]


async def test_audience_not_configured(mcp_client, seeded_db, deps):
    deps.config = deps.config.model_copy(update={"groups": {"mgmt": deps.config.groups["mgmt"]}})
    res = await mcp_client.call(alice_all, "ingest_record", ARGS)
    assert res.is_error and res.text.endswith(AUDIENCE_NOT_HELD)


async def test_naive_time_is_refused(mcp_client, seeded_db, blobs):
    for bad in ("2026-03-04T10:00:00", "not a time", ""):
        res = await mcp_client.call(alice_all, "ingest_record", {**ARGS, "occurred_at": bad})
        assert res.is_error and res.text.endswith(BAD_ARG.format(name="occurred_at")), bad
    assert not blobs.objects


async def test_too_large(mcp_client, seeded_db, deps, blobs):
    deps.settings = deps.settings.model_copy(update={"max_record_bytes": 10})
    ok = await mcp_client.call(alice_all, "ingest_record", {**ARGS, "body": "a" * 10})
    assert not ok.is_error, ok.text
    res = await mcp_client.call(alice_all, "ingest_record", {**ARGS, "source_ref": "m-2", "body": "é" * 6})
    assert res.is_error and res.text.endswith(TOO_LARGE)
    assert len(blobs.objects) == 1


async def test_unknown_label(mcp_client, seeded_db, fresh_db, blobs):
    res = await mcp_client.call(alice_all, "ingest_record", {**ARGS, "labels": ["ember", "nope"]})
    assert res.is_error and res.text.endswith(BAD_LABEL)
    assert not blobs.objects
    assert _rows(fresh_db, "SELECT count(*) FROM kh.records WHERE source_ref = 'm-1'") == [(0,)]


async def test_s3_failure_leaves_no_row(mcp_client, seeded_db, fresh_db, deps):
    deps.blobs = FailingBlobStore()
    res = await mcp_client.call(alice_all, "ingest_record", ARGS)
    assert res.is_error and "internal error" in res.text
    assert _rows(fresh_db, "SELECT count(*) FROM kh.records WHERE source_ref = 'm-1'") == [(0,)]


async def test_embed_job_is_queued(mcp_client, seeded_db, fresh_db):
    res = await mcp_client.call(alice_all, "ingest_record", ARGS)
    rows = _rows(
        fresh_db,
        "SELECT kind FROM kh.index_jobs WHERE kind = 'embed_record' AND payload->>'id' = %s",
        res.data["id"][4:],
    )
    assert rows == [("embed_record",)]


async def test_record_by_workflow_token_is_invisible_without_the_audience(mcp_client, seeded_db):
    res = await mcp_client.call(wf_token, "ingest_record", ARGS)
    assert not res.is_error, res.text
    assert not (await mcp_client.call(alice_all, "get_doc", {"id": res.data["id"]})).is_error
    for outsider in (dave_none, bob_mgmt):
        got = await mcp_client.call(outsider, "get_doc", {"id": res.data["id"]})
        assert got.is_error and got.text.endswith("not found")
    # the same key in another audience is a different record, and the outsider learns nothing
    other = await mcp_client.call(bob_mgmt, "ingest_record", {**ARGS, "audience": "mgmt"})
    assert not other.is_error and other.data["existed"] is False
