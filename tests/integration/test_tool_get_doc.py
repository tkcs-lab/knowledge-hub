"""get_doc: documents and records, hidden rows equal missing rows, bad ids."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from knowledge_hub.mcp.errors import BAD_ID, NOT_FOUND
from tests.support.blobs import MemoryBlobStore
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, dave_none, wf_token
from tests.support.seed import WHEN

pytestmark = pytest.mark.integration


@pytest.fixture
def blobs() -> MemoryBlobStore:
    return MemoryBlobStore()


@pytest.fixture
def deps(deps, blobs, kh_config):
    deps.blobs = blobs
    deps.config = kh_config.model_copy(update={"forgejo_web_url": "https://forgejo.test.example/"})
    return deps


async def test_get_doc_returns_the_document(mcp_client, seeded_db):
    key = seeded_db["documents"]["all"]
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{key}"})
    assert not res.is_error, res.text
    assert res.data == {
        "id": f"doc:{key}",
        "tier": "T0",
        "repo": "kb-all",
        "path": "all.md",
        "audience": "all",
        "labels": [],
        "title": "Doc all",
        "body": "Body all",
        "review_by": "2027-01-01",
        "stale": False,
        "commit_sha": "c1",
        "blob_sha": "blob-all",
        "supersedes": [],
        "superseded_by": "",
        "outdated": False,
        "source_link": "https://forgejo.test.example/kh/kb-all/src/commit/c1/all.md",
    }


async def test_source_link_is_empty_without_a_web_url(mcp_client, deps, seeded_db, kh_config):
    deps.config = kh_config
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{seeded_db['documents']['all']}"})
    assert res.data["source_link"] == ""


async def test_get_doc_returns_the_record_body_from_the_blob_store(mcp_client, seeded_db, blobs, fresh_db):
    key = seeded_db["records"]["all"]
    blobs.objects[f"records/{key}"] = (b"full record body", "text/plain")
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:  # point the row at the stored blob
        conn.execute("UPDATE kh.records SET blob_key = %s WHERE id = %s", (f"records/{key}", key))
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"rec:{key}"})
    assert not res.is_error, res.text
    assert res.data == {
        "id": f"rec:{key}",
        "tier": "T3",
        "audience": "all",
        "labels": [],
        "title": "Rec all",
        "body": "full record body",
        "source": "seed",
        "source_ref": "ref-all",
        "source_url": "",
        "occurred_at": "2026-01-02T03:04:05+00:00",
        "written_by": "kh-bot",
        "outdated": False,
        "outdated_at": "",
        "replaced_by": "",
        "note": "",
    }
    assert "outdated_by" not in res.data


async def test_record_body_falls_back_to_the_excerpt_without_a_blob(mcp_client, seeded_db):
    key = seeded_db["records"]["all"]
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"rec:{key}"})
    assert res.data["body"] == "excerpt"


@pytest.mark.parametrize("who", [bob_mgmt, dave_none, wf_token])
async def test_hidden_equals_missing(mcp_client, seeded_db, who):
    hr_doc = f"doc:{seeded_db['documents']['hr']}"
    hr_rec = f"rec:{seeded_db['records']['hr']}"
    for hidden, missing in ((hr_doc, f"doc:{uuid.uuid4()}"), (hr_rec, f"rec:{uuid.uuid4()}")):
        a = await mcp_client.call(who, "get_doc", {"id": hidden})
        b = await mcp_client.call(who, "get_doc", {"id": missing})
        assert a.is_error and b.is_error
        assert a.text == b.text
        assert a.text.endswith(NOT_FOUND)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "x",
        "doc:",
        "doc:not-a-uuid",
        "raw:" + str(uuid.uuid4()),
        str(uuid.uuid4()),
        "doc:" + str(uuid.uuid4()).upper(),
    ],
)
async def test_bad_ids(mcp_client, seeded_db, bad):
    res = await mcp_client.call(alice_all, "get_doc", {"id": bad})
    assert res.is_error and res.text.endswith(BAD_ID)


async def test_get_doc_shows_the_supersede_marks_of_a_document(mcp_client, seeded_db, fresh_db):
    old, new = seeded_db["documents"]["all"], seeded_db["documents"]["mgmt"]
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        conn.execute("UPDATE kh.documents SET superseded_by = %s WHERE id = %s", (new, old))
        conn.execute("UPDATE kh.documents SET supersedes = %s WHERE id = %s", (["doc-all"], new))
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{old}"})
    assert (res.data["superseded_by"], res.data["outdated"], res.data["supersedes"]) == (
        f"doc:{new}", True, [])
    res = await mcp_client.call(bob_mgmt, "get_doc", {"id": f"doc:{new}"})
    assert (res.data["superseded_by"], res.data["outdated"], res.data["supersedes"]) == (
        "", False, ["doc-all"])


async def test_get_doc_shows_the_outdated_marks_of_a_record(mcp_client, seeded_db, fresh_db):
    key = seeded_db["records"]["all"]
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        conn.execute(
            "UPDATE kh.records SET outdated = true, outdated_at = %s, outdated_by = 'alice', "
            "outdated_note = 'old', replaced_by = 'doc:x' WHERE id = %s", (WHEN, key))
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"rec:{key}"})
    assert (res.data["outdated"], res.data["outdated_at"], res.data["replaced_by"], res.data["note"]) == (
        True, WHEN.isoformat(), "doc:x", "old")
    assert "outdated_by" not in res.data
