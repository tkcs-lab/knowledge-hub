"""move_item for records: the move, its refusals, search afterwards and the log line."""

from __future__ import annotations

import logging

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.server import Deps
from tests.support.bearer import TestBearerResolver
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, carol_hr, multi, wf_token
from tests.support.seed import add_chunked_record, add_record

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


async def move(mcp_client, caller, id, from_group="all", to_group="mgmt"):
    return await mcp_client.call(
        caller, "move_item", {"id": id, "from_group": from_group, "to_group": to_group})


async def test_move_changes_group_and_keeps_id(mcp_client, db):
    rec = add_record(db)
    db.commit()
    res = await move(mcp_client, multi, f"rec:{rec}")
    assert not res.is_error, res.text
    assert res.data == {"tier": "T3", "id": f"rec:{rec}", "from_group": "all", "to_group": "mgmt",
                        "moved": True}
    assert db.execute("SELECT audience FROM kh.records WHERE id = %s", (rec,)).fetchone() == ("mgmt",)
    db.commit()


async def test_search_finds_moved_record_only_in_new_group(mcp_client, db):
    rec = add_chunked_record(db, "quokka migration notes", audience="all")
    db.commit()
    assert not (await move(mcp_client, multi, f"rec:{rec}")).is_error
    ids = [h["id"] for h in (await mcp_client.call(alice_all, "search", {"query": "quokka"})).data["hits"]]
    assert f"rec:{rec}" not in ids
    ids = [h["id"] for h in (await mcp_client.call(multi, "search", {"query": "quokka"})).data["hits"]]
    assert f"rec:{rec}" in ids


async def test_move_needs_both_groups(mcp_client, db):
    rec = add_record(db)
    db.commit()
    res = await move(mcp_client, alice_all, f"rec:{rec}")
    assert res.is_error and res.text.endswith(errors.AUDIENCE_NOT_HELD)


async def test_wrong_from_group(mcp_client, db):
    rec = add_record(db, audience="mgmt")
    db.commit()
    res = await move(mcp_client, multi, f"rec:{rec}", "all", "mgmt")
    assert res.is_error and res.text.endswith(errors.MOVE_WRONG_GROUP)


async def test_same_group_is_bad_arg(mcp_client, db):
    rec = add_record(db)
    db.commit()
    res = await move(mcp_client, multi, f"rec:{rec}", "all", "all")
    assert res.is_error and res.text.endswith(errors.BAD_ARG.format(name="to_group"))


async def test_hidden_record_is_not_found(mcp_client, db):
    rec = add_record(db, audience="hr")
    db.commit()
    res = await move(mcp_client, multi, f"rec:{rec}", "all", "mgmt")
    assert res.is_error and res.text.endswith(errors.NOT_FOUND)
    res = await move(mcp_client, multi, "rec:00000000-0000-4000-8000-000000000000")
    assert res.is_error and res.text.endswith(errors.NOT_FOUND)


async def test_target_with_same_source_ref_is_conflict(mcp_client, db):
    rec = add_record(db, source="meet", source_ref="same")
    add_record(db, audience="mgmt", source="meet", source_ref="same")
    db.commit()
    res = await move(mcp_client, multi, f"rec:{rec}")
    assert res.is_error and res.text.endswith(errors.MOVE_CONFLICT)
    assert db.execute("SELECT audience FROM kh.records WHERE id = %s", (rec,)).fetchone() == ("all",)
    db.commit()


async def test_linked_record_is_refused(mcp_client, db):
    rec, other = add_record(db), add_record(db)
    db.commit()
    res = await mcp_client.call(alice_all, "mark_record_outdated",
                                {"id": f"rec:{other}", "replaced_by": f"rec:{rec}"})
    assert not res.is_error, res.text
    res = await move(mcp_client, multi, f"rec:{rec}")
    assert res.is_error and res.text.endswith(errors.MOVE_LINKED)


async def test_workflow_principal_is_refused(mcp_client, db):
    rec = add_record(db)
    db.commit()
    res = await move(mcp_client, wf_token, f"rec:{rec}")
    assert res.is_error and res.text.endswith(errors.NOT_FOR_WORKFLOWS)
    res = await move(mcp_client, carol_hr, f"rec:{rec}")
    assert res.is_error and res.text.endswith(errors.AUDIENCE_NOT_HELD)


async def test_log_line(mcp_client, db, caplog):
    rec = add_record(db)
    db.commit()
    with caplog.at_level(logging.INFO, logger="knowledge_hub.tools"):
        res = await move(mcp_client, multi, f"rec:{rec}")
    assert not res.is_error, res.text
    assert f"move_item sub=multi id=rec:{rec} from=all to=mgmt" in caplog.text
