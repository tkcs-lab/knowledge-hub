"""write_derived and the embed_derived job (plan 3.7, 3.9)."""

import json
import uuid

import psycopg
import pytest

from knowledge_hub.config import Config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.worker import Worker
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools import derived as derived_tool
from knowledge_hub.visibility import Visibility, can_read
from tests.integration.test_derived_visibility_sql import doc as make_doc
from tests.integration.test_derived_visibility_sql import raw as make_raw
from tests.support.blobs import MemoryBlobStore
from tests.support.pg import FRESH_DB
from tests.support.principals import PRINCIPALS, alice_all, bob_mgmt, dave_none, wf_token
from tests.support.seed import CASES

DAVE = "dave@example.org"


@pytest.fixture
def blobs():
    return MemoryBlobStore()


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool, blobs):
    return Deps(
        settings=app_settings, config=Config(), app_pool=app_pool, indexer_pool=indexer_pool, blobs=blobs
    )


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


def args(sources, **over):
    base = {
        "kind": "summary",
        "title": "Sum",
        "body": "# Summary\n\nThe team agreed on the plan.",
        "domain": "technical",
        "claims_json": "[]",
        "derived_from": list(sources),
        "pipeline": "p",
        "model": "m",
        "pipeline_version": "1",
    }
    base.update(over)
    return base


def derived_rows(admin, did):
    return admin.execute(
        "SELECT required_audiences, allowed_emails, created_by_sub, created_by_type, kind, domain,"
        " generated_by"
        " FROM kh.derived_records WHERE id = %s",
        (did,),
    ).fetchall()


def ok(res) -> str:
    assert not res.is_error, res
    cid = res.data["citation_id"]
    assert cid.startswith("derived:")
    return cid.removeprefix("derived:")


def err(res, text) -> None:
    assert res.status == 200 and res.is_error and res.text.endswith(text), res


# --- hidden sources ----------------------------------------------------------------------------


@pytest.mark.parametrize("caller", [alice_all, dave_none, wf_token], ids=lambda c: c.sub)
@pytest.mark.parametrize("hidden", sorted(c for c in CASES if c != "all"))
async def test_hidden_source_reads_like_a_missing_one(admin, mcp_client, caller, hidden):
    audiences, emails = CASES[hidden]
    if can_read(Visibility(audiences, emails), caller.audiences, caller.email):
        pytest.skip("visible to this caller")
    d = make_doc(admin, audiences, emails)
    r = make_raw(admin, audiences, emails)
    for source in (f"doc:{d}", f"raw:{r}", f"doc:{d}@c1", f"doc:{d}@wrong"):
        res = await mcp_client.call(caller, "write_derived", args([source]))
        missing_source = f"doc:{uuid.uuid4()}" + ("@wrong" if "@" in source else "")
        missing = await mcp_client.call(caller, "write_derived", args([missing_source]))
        err(res, "cannot write derived record: 1 cited sources are not found or not visible to you")
        assert (res.status, res.is_error, res.text) == (missing.status, missing.is_error, missing.text)
        assert "wrong" not in res.text and "c1" not in res.text
    assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone() == (0,)


async def test_one_hidden_among_visible_sources_refuses_all(admin, mcp_client):
    seen, hidden = make_doc(admin, ("all",)), make_doc(admin, ("mgmt",))
    res = await mcp_client.call(alice_all, "write_derived", args([f"doc:{seen}", f"doc:{hidden}"]))
    err(res, "1 cited sources are not found or not visible to you")
    assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone() == (0,)


# --- versions ----------------------------------------------------------------------------------


async def test_correct_version_on_a_visible_doc_passes_and_is_stored(admin, mcp_client):
    d = make_doc(admin, ("all",), sha="abc123")
    res = await mcp_client.call(alice_all, "write_derived", args([f"doc:{d}@abc123"]))
    did = ok(res)
    assert admin.execute(
        "SELECT source_kind, source_id, version, src_required_audiences, src_allowed_emails"
        " FROM kh.derived_sources WHERE derived_id = %s",
        (did,),
    ).fetchall() == [("doc", d, "abc123", ["all"], None)]


async def test_stale_version_is_version_changed(admin, mcp_client):
    d = make_doc(admin, ("all",), sha="new1")
    res = await mcp_client.call(alice_all, "write_derived", args([f"doc:{d}@old1"]))
    err(res, f"source doc:{d} changed since you read it (now new1)")
    assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone() == (0,)


async def test_version_without_pin_stores_the_current_one(admin, mcp_client):
    d = make_doc(admin, ("all",), sha="cur9")
    did = ok(await mcp_client.call(alice_all, "write_derived", args([f"doc:{d}"])))
    assert admin.execute(
        "SELECT version FROM kh.derived_sources WHERE derived_id = %s", (did,)
    ).fetchone() == ("cur9",)


async def test_version_on_a_raw_id_is_bad_version(admin, mcp_client):
    r = make_raw(admin, ("all",))
    err(
        await mcp_client.call(alice_all, "write_derived", args([f"raw:{r}@abc"])),
        "@version is only allowed on doc: ids",
    )
    # Checked before visibility: a hidden or missing raw id gives the same text.
    err(
        await mcp_client.call(dave_none, "write_derived", args([f"raw:{uuid.uuid4()}@abc"])),
        "@version is only allowed on doc: ids",
    )


# --- argument checks ---------------------------------------------------------------------------


async def test_argument_errors(admin, mcp_client):
    d = make_doc(admin, ("all",))
    good = f"doc:{d}"
    call = mcp_client.call
    err(await call(alice_all, "write_derived", args(["doc:nope"])), "malformed id doc:nope")
    err(
        await call(alice_all, "write_derived", args([good, f"doc:{d}@a b"])), "malformed id doc:" + d + "@a b"
    )
    err(
        await call(alice_all, "write_derived", args([good], kind="essay")),
        "kind must be one of summary, extracted-fact, decision-candidate, changelog",
    )
    many = [f"doc:{uuid.uuid4()}" for _ in range(51)]
    err(await call(alice_all, "write_derived", args(many)), "at most 50 sources")
    err(
        await call(alice_all, "write_derived", args([])),
        "0 cited sources are not found or not visible to you",
    )
    assert (await call(alice_all, "write_derived", args([good], domain="nope"))).is_error
    # the same source twice is one source
    did = ok(await call(alice_all, "write_derived", args([good, good.upper().replace("DOC", "doc")])))
    count = admin.execute("SELECT count(*) FROM kh.derived_sources WHERE derived_id = %s", (did,)).fetchone()
    assert count == (1,)


# --- stored visibility -------------------------------------------------------------------------


async def test_visibility_is_the_union_of_the_sources(admin, mcp_client):
    a, m = make_doc(admin, ("all",)), make_doc(admin, ("mgmt",))
    r = make_raw(admin, ("hr",))
    did = ok(await mcp_client.call(bob_mgmt, "write_derived", args([f"doc:{a}", f"doc:{m}"])))
    rows = derived_rows(admin, did)
    assert rows == [
        (
            ["all", "mgmt"],
            None,
            "bob",
            "claude_client",
            "summary",
            "technical",
            {"pipeline": "p", "model": "m", "pipeline_version": "1"},
        )
    ]
    # the hidden hr raw record stays hidden from bob
    assert (await mcp_client.call(bob_mgmt, "write_derived", args([f"raw:{r}"]))).is_error
    # alice cannot read the mgmt-wide row, bob can
    assert (await mcp_client.call(alice_all, "get_doc", {"id": f"derived:{did}"})).is_error
    assert not (await mcp_client.call(bob_mgmt, "get_doc", {"id": f"derived:{did}"})).is_error


async def test_email_sources_intersect(admin, mcp_client):
    both = make_doc(admin, (), [DAVE, "x@example.org"])
    only = make_doc(admin, (), [DAVE])
    did = ok(await mcp_client.call(dave_none, "write_derived", args([f"doc:{both}", f"doc:{only}"])))
    assert derived_rows(admin, did)[0][:2] == ([], [DAVE])


# --- the provenance exception ------------------------------------------------------------------


def ingest(source_ref, participants):
    return {
        "source": "meet",
        "source_ref": source_ref,
        "title": "T",
        "body": "text",
        "domain": "technical",
        "occurred_at": "2026-01-02T03:04:05+00:00",
        "participants": participants,
    }


async def test_workflow_token_cites_its_own_attendee_only_record(admin, mcp_client):
    res = await mcp_client.call(
        wf_token, "ingest_raw", ingest("m-1", ["Dave@Example.org", "Zed@example.org"])
    )
    rid = res.data["citation_id"]
    out = await mcp_client.call(wf_token, "write_derived", args([rid]))
    did = ok(out)
    assert derived_rows(admin, did)[0][:3] == ([], ["dave@example.org", "zed@example.org"], wf_token.sub)
    # the writer can read neither row afterwards
    for cid in (rid, f"derived:{did}"):
        err(await mcp_client.call(wf_token, "get_doc", {"id": cid}), "not found")
    # a participant can read the derived row
    assert not (await mcp_client.call(dave_none, "get_doc", {"id": f"derived:{did}"})).is_error


async def test_workflow_token_cannot_cite_another_tokens_record(admin, mcp_client):
    r = make_raw(admin, None, sub="n8n-token:other", participants=(DAVE,))
    res = await mcp_client.call(wf_token, "write_derived", args([f"raw:{r}"]))
    err(res, "1 cited sources are not found or not visible to you")
    assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone() == (0,)


async def test_exception_does_not_cover_a_person(admin, mcp_client):
    # alice is not an n8n workflow, even if the sub were the same
    r = make_raw(admin, None, sub=alice_all.sub, participants=(DAVE,))
    err(
        await mcp_client.call(alice_all, "write_derived", args([f"raw:{r}"])),
        "not found or not visible to you",
    )


async def test_own_records_sharing_no_attendee_are_refused(admin, mcp_client):
    r1 = (await mcp_client.call(wf_token, "ingest_raw", ingest("m-1", ["a@example.org"]))).data["citation_id"]
    r2 = (await mcp_client.call(wf_token, "ingest_raw", ingest("m-2", ["b@example.org"]))).data["citation_id"]
    res = await mcp_client.call(wf_token, "write_derived", args([r1, r2]))
    err(res, "the cited sources share no attendee, so nobody could read it")
    assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone() == (0,)


async def test_own_record_with_an_open_source_gives_the_open_audience(admin, mcp_client):
    r = (await mcp_client.call(wf_token, "ingest_raw", ingest("m-1", ["a@example.org"]))).data["citation_id"]
    d = make_doc(admin, ("all",))
    did = ok(await mcp_client.call(wf_token, "write_derived", args([r, f"doc:{d}"])))
    assert derived_rows(admin, did)[0][:2] == (["all"], ["a@example.org"])


# --- claims ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claims",
    [
        "not json",
        "{}",
        "[1]",
        '[{"source_ids": ["@D"], "quote": "q"}]',  # no text
        '[{"text": "t", "source_ids": ["@D"]}]',  # neither quote nor timestamp
        '[{"text": "t", "quote": "q"}]',  # no source_ids
        '[{"text": "t", "source_ids": [], "quote": "q"}]',
        '[{"text": "t", "source_ids": ["@OTHER"], "quote": "q"}]',  # not cited
        '[{"text": "t", "source_ids": ["doc:zzz"], "quote": "q"}]',
        '[{"text": "t", "source_ids": ["@D"], "quote": 5}]',
        '[{"text": 3, "source_ids": ["@D"], "quote": "q"}]',
    ],
)
async def test_bad_claims(admin, mcp_client, claims):
    d = make_doc(admin, ("all",))
    claims = claims.replace("@D", f"doc:{d}").replace("@OTHER", f"doc:{uuid.uuid4()}")
    res = await mcp_client.call(alice_all, "write_derived", args([f"doc:{d}"], claims_json=claims))
    err(res, "claims_json must be a JSON list of objects with text, source_ids and a quote or timestamp")
    assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone() == (0,)


async def test_good_claims_are_stored(admin, mcp_client):
    d = make_doc(admin, ("all",))
    claims = [
        {"text": "a", "source_ids": [f"doc:{d}"], "quote": "q"},
        {"text": "b", "source_ids": [f"doc:{d}@c1"], "timestamp": "00:01:02"},
    ]
    did = ok(
        await mcp_client.call(alice_all, "write_derived", args([f"doc:{d}"], claims_json=json.dumps(claims)))
    )
    assert admin.execute("SELECT claims FROM kh.derived_records WHERE id = %s", (did,)).fetchone() == (
        claims,
    )


# --- the commit-time check ---------------------------------------------------------------------


async def test_visibility_mismatch_is_an_internal_error(admin, mcp_client, monkeypatch, caplog):
    monkeypatch.setattr(
        derived_tool,
        "_INSERT_RECORD",
        derived_tool._INSERT_RECORD.replace("%s, %s)", "%s || ARRAY['zz'], %s)"),
    )
    d = make_doc(admin, ("all",))
    res = await mcp_client.call(alice_all, "write_derived", args([f"doc:{d}"]))
    assert (
        res.is_error and "internal error (ref " in res.text and "zz" not in res.text and "all" not in res.text
    )
    assert "does not match" in caplog.text or "P0K04" in caplog.text
    assert admin.execute("SELECT count(*) FROM kh.derived_records").fetchone() == (0,)


# --- the matrix --------------------------------------------------------------------------------


@pytest.mark.parametrize("principal", sorted(PRINCIPALS))
async def test_every_hidden_seed_row_reads_like_a_missing_one(seeded_db, mcp_client, principal):
    caller = PRINCIPALS[principal]
    missing = await mcp_client.call(caller, "write_derived", args([f"doc:{uuid.uuid4()}"]))
    for case, (audiences, emails) in CASES.items():
        if can_read(Visibility(audiences, emails), caller.audiences, caller.email):
            continue
        for table, prefix in (("documents", "doc"), ("raw_records", "raw"), ("derived_records", "derived")):
            res = await mcp_client.call(caller, "write_derived", args([f"{prefix}:{seeded_db[table][case]}"]))
            assert (res.status, res.is_error, res.text) == (missing.status, missing.is_error, missing.text)


# --- embed_derived -----------------------------------------------------------------------------


async def test_embed_derived_job_writes_chunks_with_the_row_visibility(admin, mcp_client, indexer_pool):
    d = make_doc(admin, ("all",))
    body = "# Summary\n\nThe team agreed on the plan.\n\n## More\n\n" + "word " * 100
    did = ok(await mcp_client.call(alice_all, "write_derived", args([f"doc:{d}"], body=body)))
    assert admin.execute("SELECT kind, state, payload FROM kh.index_jobs").fetchall() == [
        ("embed_derived", "queued", {"id": did})
    ]
    worker = Worker(indexer_pool, None, FakeEmbedder(), Config())
    assert await worker.run_once() is True
    assert admin.execute("SELECT state FROM kh.index_jobs").fetchone() == ("done",)
    rows = admin.execute(
        "SELECT owner_key, tier, required_audiences, allowed_emails, embedding IS NOT NULL,"
        " embedding_model, text"
        " FROM kh.chunks WHERE derived_id = %s ORDER BY ordinal",
        (did,),
    ).fetchall()
    assert len(rows) >= 2 and all(
        r[:6] == (f"derived:{did}", "T2", ["all"], None, True, "fake") for r in rows
    )
    assert "The team agreed on the plan." in rows[0][6]
    assert await worker.run_once() is False
    # a retry replaces the chunks instead of duplicating them
    admin.execute(
        "INSERT INTO kh.index_jobs (kind, payload) VALUES ('embed_derived', %s::jsonb)",
        (json.dumps({"id": did}),),
    )
    assert await worker.run_once() is True
    count = admin.execute("SELECT count(*) FROM kh.chunks WHERE derived_id = %s", (did,)).fetchone()
    assert count == (len(rows),)


async def test_embed_derived_for_a_deleted_row_is_a_no_op(admin, indexer_pool):
    admin.execute(
        "INSERT INTO kh.index_jobs (kind, payload) VALUES ('embed_derived', %s::jsonb)",
        (json.dumps({"id": str(uuid.uuid4())}),),
    )
    assert await Worker(indexer_pool, None, FakeEmbedder(), Config()).run_once() is True
    assert admin.execute("SELECT state FROM kh.index_jobs").fetchone() == ("done",)


async def test_attendee_only_derived_chunks_keep_the_emails(admin, mcp_client, indexer_pool):
    rid = (await mcp_client.call(wf_token, "ingest_raw", ingest("m-1", [DAVE]))).data["citation_id"]
    did = ok(await mcp_client.call(wf_token, "write_derived", args([rid])))
    worker = Worker(indexer_pool, None, FakeEmbedder(), Config(), MemoryBlobStore())
    while await worker.run_once():
        pass
    rows = admin.execute(
        "SELECT DISTINCT required_audiences, allowed_emails FROM kh.chunks WHERE derived_id = %s", (did,)
    ).fetchall()
    assert rows == [([], [DAVE])]


async def test_the_same_doc_with_two_versions_is_refused(admin, mcp_client):
    d = make_doc(admin, ("all",), sha="c1")
    res = await mcp_client.call(alice_all, "write_derived", args([f"doc:{d}@c1", f"doc:{d}@c2"]))
    err(res, f"malformed id doc:{d}@c2")
