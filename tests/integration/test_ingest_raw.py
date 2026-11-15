"""ingest_raw, the audience map loader and the embed_raw job (plan 3.7, 3.9)."""

import psycopg
import pytest

from knowledge_hub.config import Config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.rebuild import config_hash
from knowledge_hub.indexer.worker import Worker
from knowledge_hub.jobs.audience_map import load_audience_map
from knowledge_hub.mcp.server import Deps
from tests.support.blobs import MemoryBlobStore
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, dave_none, wf_token

MAP = {"series:s-all": ["all"], "series:s-mgmt": ["mgmt"], "folder:f-all": ["all"]}
CONFIG = Config(raw_audience_map=MAP)


@pytest.fixture
def blobs():
    return MemoryBlobStore()


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool, blobs):
    return Deps(
        settings=app_settings, config=CONFIG, app_pool=app_pool, indexer_pool=indexer_pool, blobs=blobs
    )


def args(**over):
    base = {
        "source": "meet",
        "source_ref": "m-1",
        "title": "Weekly",
        "body": "# Notes\n\nWe decided on the plan.",
        "domain": "technical",
        "occurred_at": "2026-01-02T03:04:05+00:00",
    }
    base.update(over)
    return base


def sql(fresh_db, query, params=()):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else []


def raw_id(res):
    assert not res.is_error, res
    return res.data["citation_id"].removeprefix("raw:")


async def test_loader_replaces_rows_and_returns_the_hash(fresh_db, indexer_pool):
    sql(fresh_db, "INSERT INTO kh.raw_audience_map VALUES ('series', 'old', '{all}')")
    assert await load_audience_map(indexer_pool, CONFIG) == config_hash(CONFIG)
    rows = sql(fresh_db, "SELECT kind, key, audiences FROM kh.raw_audience_map ORDER BY kind, key")
    assert rows == [
        ("folder", "f-all", ["all"]),
        ("series", "s-all", ["all"]),
        ("series", "s-mgmt", ["mgmt"]),
    ]


async def test_mapped_series_gives_its_audiences(fresh_db, mcp_client):
    res = await mcp_client.call(wf_token, "ingest_raw", args(series_id="s-all"))
    rid = raw_id(res)
    assert sql(
        fresh_db, "SELECT required_audiences, allowed_emails, ingested_by_sub, ingested_by_type, blob_key"
        " FROM kh.raw_records WHERE id = %s", (rid,)
    ) == [(["all"], None, wf_token.sub, "n8n_workflow", f"raw/{rid}")]


async def test_series_the_token_does_not_hold_is_refused(fresh_db, mcp_client):
    res = await mcp_client.call(wf_token, "ingest_raw", args(series_id="s-mgmt"))
    assert res.is_error and "your token does not hold the audience" in res.text
    assert sql(fresh_db, "SELECT count(*) FROM kh.raw_records") == [(0,)]


async def test_unmapped_gives_lowercased_participants(fresh_db, mcp_client):
    bad = args(series_id="nope", participants=["Dave@Example.org"])
    res = await mcp_client.call(wf_token, "ingest_raw", bad)
    rid = raw_id(res)
    query = "SELECT required_audiences, allowed_emails FROM kh.raw_records WHERE id = %s"
    assert sql(fresh_db, query, (rid,)) == [([], ["dave@example.org"])]


async def test_unmapped_without_participants_is_refused(mcp_client):
    res = await mcp_client.call(wf_token, "ingest_raw", args())
    assert res.is_error and "no audience mapping for series or folder and no participants" in res.text


async def test_a_person_is_forbidden(fresh_db, mcp_client):
    res = await mcp_client.call(alice_all, "ingest_raw", args(series_id="s-all"))
    assert res.is_error and res.text.endswith("ingest_raw is limited to workflow principals")
    assert sql(fresh_db, "SELECT count(*) FROM kh.raw_records") == [(0,)]


async def test_writer_cannot_read_back_an_attendee_only_row(mcp_client):
    res = await mcp_client.call(wf_token, "ingest_raw", args(participants=["dave@example.org"]))
    rid = raw_id(res)
    back = await mcp_client.call(wf_token, "get_doc", {"id": f"raw:{rid}"})
    assert back.is_error and back.text.endswith("not found")
    assert not (await mcp_client.call(dave_none, "get_doc", {"id": f"raw:{rid}"})).is_error


async def test_oversize_body_is_rejected(fresh_db, mcp_client, app_settings, deps, blobs):
    deps.settings = app_settings.model_copy(update={"max_raw_bytes": 10})
    res = await mcp_client.call(wf_token, "ingest_raw", args(series_id="s-all", body="x" * 11))
    assert res.is_error and res.text.endswith("body too large (limit 10 bytes)")
    assert sql(fresh_db, "SELECT count(*) FROM kh.raw_records") == [(0,)] and blobs.objects == {}


async def test_bad_domain_and_time_are_rejected(mcp_client):
    for bad in (args(series_id="s-all", domain="nope"), args(series_id="s-all", occurred_at="2026-01-02")):
        assert (await mcp_client.call(wf_token, "ingest_raw", bad)).is_error


async def test_second_identical_ingest_is_already_ingested(fresh_db, mcp_client, blobs):
    first = await mcp_client.call(wf_token, "ingest_raw", args(series_id="s-all"))
    rid = raw_id(first)
    again = await mcp_client.call(wf_token, "ingest_raw", args(series_id="s-all"))
    assert again.is_error and again.text.endswith(f"already ingested: raw:{rid}")
    assert sql(fresh_db, "SELECT count(*) FROM kh.raw_records") == [(1,)]
    assert list(blobs.objects) == [f"raw/{rid}"]


async def test_failed_upload_leaves_no_row(fresh_db, mcp_client, blobs, monkeypatch):
    async def boom(*a, **k):
        raise OSError("store down")

    monkeypatch.setattr(blobs, "put", boom)
    res = await mcp_client.call(wf_token, "ingest_raw", args(series_id="s-all"))
    assert res.is_error and "internal error" in res.text and "store down" not in res.text
    assert sql(fresh_db, "SELECT count(*) FROM kh.raw_records") == [(0,)]


async def test_embed_raw_job_writes_chunks_with_the_row_visibility(fresh_db, mcp_client, indexer_pool, blobs):
    rid = raw_id(await mcp_client.call(wf_token, "ingest_raw", args(series_id="s-all")))
    assert sql(fresh_db, "SELECT kind, state FROM kh.index_jobs") == [("embed_raw", "queued")]
    worker = Worker(indexer_pool, None, FakeEmbedder(), CONFIG, blobs)
    assert await worker.run_once() is True
    assert sql(fresh_db, "SELECT state FROM kh.index_jobs") == [("done",)]
    rows = sql(
        fresh_db,
        "SELECT owner_key, tier, required_audiences, allowed_emails, embedding IS NOT NULL, text"
        " FROM kh.chunks WHERE raw_id = %s",
        (rid,),
    )
    assert rows and all(r[:5] == (f"raw:{rid}", "T3", ["all"], None, True) for r in rows)
    assert "We decided on the plan." in rows[0][5]
    assert await worker.run_once() is False


async def test_get_doc_returns_the_blob_text_to_visible_callers_only(mcp_client):
    long_body = "# Notes\n\n" + "word " * 1000
    rid = raw_id(await mcp_client.call(wf_token, "ingest_raw", args(series_id="s-all", body=long_body)))
    seen = await mcp_client.call(alice_all, "get_doc", {"id": f"raw:{rid}"})
    assert seen.data["text"] == long_body and len(long_body) > 2000
    hidden = raw_id(await mcp_client.call(wf_token, "ingest_raw", args(source_ref="m-2", series_id="s-all")))
    private = args(source_ref="m-3", participants=["dave@example.org"])
    other = await mcp_client.call(wf_token, "ingest_raw", private)
    res = await mcp_client.call(bob_mgmt, "get_doc", {"id": f"raw:{raw_id(other)}"})
    assert res.is_error and res.text.endswith("not found")
    assert hidden
