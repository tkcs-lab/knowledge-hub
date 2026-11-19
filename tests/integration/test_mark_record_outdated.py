"""mark_record_outdated: mark and unmark, replaced_by checks, hidden-equals-missing, limits, logging."""

from __future__ import annotations

import logging
import uuid

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.errors import BAD_ARG, NOT_FOUND
from knowledge_hub.mcp.server import Deps
from tests.support.bearer import TestBearerResolver
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, multi, wf_token
from tests.support.seed import add_chunked_record, add_doc, add_record

pytestmark = pytest.mark.integration


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
        embedder=FakeEmbedder(), bearer=TestBearerResolver(kh_config),
    )


@pytest.fixture
def db(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        yield conn
        conn.commit()


def bad(name: str) -> str:
    return BAD_ARG.format(name=name)


async def mark(mcp_client, caller, **args):
    return await mcp_client.call(caller, "mark_record_outdated", args)


async def test_mark_and_unmark(mcp_client, db):
    rec = add_chunked_record(db, "quokka facts", audience="all")
    db.commit()
    rid = f"rec:{rec}"

    res = await mark(mcp_client, alice_all, id=rid, note="old news")
    assert not res.is_error, res.text
    assert res.data == {"id": rid, "outdated": True, "replaced_by": ""}
    got = await mcp_client.call(alice_all, "get_doc", {"id": rid})
    assert got.data["outdated"] is True and got.data["note"] == "old news"
    found = await mcp_client.call(alice_all, "search", {"query": "quokka"})
    assert rid not in [h["id"] for h in found.data["hits"]]

    res = await mark(mcp_client, alice_all, id=rid, outdated=False)
    assert not res.is_error, res.text
    assert res.data == {"id": rid, "outdated": False, "replaced_by": ""}
    got = await mcp_client.call(alice_all, "get_doc", {"id": rid})
    assert got.data["outdated"] is False and got.data["note"] == ""
    assert got.data["outdated_at"] == "" and got.data["replaced_by"] == ""
    found = await mcp_client.call(alice_all, "search", {"query": "quokka"})
    assert rid in [h["id"] for h in found.data["hits"]]


async def test_replaced_by_a_same_group_record_and_a_document(mcp_client, db):
    rec, other, doc = add_record(db), add_record(db), add_doc(db)
    db.commit()
    for successor in (f"rec:{other}", f"doc:{doc}"):
        res = await mark(mcp_client, alice_all, id=f"rec:{rec}", replaced_by=successor)
        assert not res.is_error, res.text
        assert res.data["replaced_by"] == successor
        got = await mcp_client.call(alice_all, "get_doc", {"id": f"rec:{rec}"})
        assert got.data["outdated"] is True and got.data["replaced_by"] == successor


async def test_replaced_by_other_group_missing_and_malformed_all_answer_bad_arg_replaced_by(
    mcp_client, db
):
    rec = add_record(db)
    other_group = add_doc(db, repo="kb-mgmt", audience="mgmt")
    db.commit()
    texts = set()
    for successor in (f"doc:{other_group}", f"rec:{uuid.uuid4()}", "doc:nonsense", f"rec:{rec}"):
        res = await mark(mcp_client, alice_all, id=f"rec:{rec}", replaced_by=successor)
        assert res.is_error
        texts.add(res.text)
    assert texts == {f"Error executing tool mark_record_outdated: {bad('replaced_by')}"}


async def test_hidden_and_missing_record_answer_not_found_identically(mcp_client, db):
    # bob holds all and mgmt, so the hidden record belongs to hr
    hr_rec = add_record(db, audience="hr")
    db.commit()
    hidden = await mark(mcp_client, bob_mgmt, id=f"rec:{hr_rec}")
    missing = await mark(mcp_client, bob_mgmt, id=f"rec:{uuid.uuid4()}")
    assert hidden.is_error and missing.is_error
    assert hidden.text == missing.text
    assert hidden.text.endswith(NOT_FOUND)


async def test_doc_id_is_refused(mcp_client, db):
    doc = add_doc(db)
    db.commit()
    for value in (f"doc:{doc}", "nonsense", ""):
        res = await mark(mcp_client, alice_all, id=value)
        assert res.is_error and res.text.endswith(bad("id"))


async def test_note_limits(mcp_client, db):
    rec = add_record(db)
    db.commit()
    ok = await mark(mcp_client, alice_all, id=f"rec:{rec}", note="n" * 500)
    assert not ok.is_error, ok.text
    for note in ("n" * 501, "a\x00b"):
        res = await mark(mcp_client, alice_all, id=f"rec:{rec}", note=note)
        assert res.is_error and res.text.endswith(bad("note"))


async def test_clearing_with_a_replaced_by_is_refused(mcp_client, db):
    rec, other = add_record(db), add_record(db)
    db.commit()
    res = await mark(mcp_client, alice_all, id=f"rec:{rec}", outdated=False, replaced_by=f"rec:{other}")
    assert res.is_error and res.text.endswith(bad("replaced_by"))
    res = await mark(mcp_client, alice_all, id=f"rec:{rec}", outdated=False, note="x")
    assert res.is_error and res.text.endswith(bad("note"))


async def test_a_second_holder_of_the_group_can_unmark(mcp_client, db):
    rec = add_record(db)
    db.commit()
    assert not (await mark(mcp_client, alice_all, id=f"rec:{rec}")).is_error
    res = await mark(mcp_client, multi, id=f"rec:{rec}", outdated=False)
    assert not res.is_error, res.text
    got = await mcp_client.call(alice_all, "get_doc", {"id": f"rec:{rec}"})
    assert got.data["outdated"] is False


async def test_n8n_workflow_token_can_mark_a_record_of_its_audience(mcp_client, db):
    rec = add_record(db, audience="all")
    db.commit()
    res = await mark(mcp_client, wf_token, id=f"rec:{rec}", note="by workflow")
    assert not res.is_error, res.text
    got = await mcp_client.call(alice_all, "get_doc", {"id": f"rec:{rec}"})
    assert got.data["outdated"] is True and got.data["note"] == "by workflow"


async def test_log_line_names_the_caller(mcp_client, db, caplog):
    rec = add_record(db)
    db.commit()
    with caplog.at_level(logging.INFO, logger="knowledge_hub.tools"):
        res = await mark(mcp_client, alice_all, id=f"rec:{rec}")
    assert not res.is_error, res.text
    assert "mark_record_outdated sub=alice" in caplog.text
