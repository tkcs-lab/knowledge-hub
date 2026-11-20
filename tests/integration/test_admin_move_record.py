"""kh move for records: the move, its refusals, search afterwards and the log line."""

from __future__ import annotations

import logging

import psycopg
import pytest

from knowledge_hub.admin.move_cmd import do_move
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import KhError
from knowledge_hub.mcp.server import Deps
from tests.support.bearer import TestBearerResolver
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, multi
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


ADMIN = "admin@example.test"


async def move(deps, id, from_group="all", to_group="mgmt"):
    return await do_move(deps, ADMIN, id, from_group, to_group)


async def refused(deps, id, from_group="all", to_group="mgmt") -> str:
    with pytest.raises(KhError) as exc:
        await move(deps, id, from_group, to_group)
    return exc.value.text


async def test_move_changes_group_and_keeps_id(deps, db):
    rec = add_record(db)
    db.commit()
    res = await move(deps, f"rec:{rec}")
    assert res == {
        "tier": "T3", "id": f"rec:{rec}", "from_group": "all", "to_group": "mgmt", "moved": True
    }
    assert db.execute("SELECT audience FROM kh.records WHERE id = %s", (rec,)).fetchone() == ("mgmt",)
    db.commit()


async def test_search_finds_moved_record_only_in_new_group(mcp_client, deps, db):
    rec = add_chunked_record(db, "quokka migration notes", audience="all")
    db.commit()
    await move(deps, f"rec:{rec}")
    ids = [h["id"] for h in (await mcp_client.call(alice_all, "search", {"query": "quokka"})).data["hits"]]
    assert f"rec:{rec}" not in ids
    ids = [h["id"] for h in (await mcp_client.call(multi, "search", {"query": "quokka"})).data["hits"]]
    assert f"rec:{rec}" in ids


async def test_move_to_an_inactive_group_is_refused(deps, db):
    rec = add_record(db)
    db.commit()
    assert await refused(deps, f"rec:{rec}", "all", "nowhere") == errors.AUDIENCE_NOT_HELD


async def test_admin_may_move_between_groups_they_do_not_hold(deps, db):
    rec = add_record(db, audience="mgmt")
    db.commit()
    res = await move(deps, f"rec:{rec}", "mgmt", "all")
    assert res["moved"] is True and res["to_group"] == "all"
    assert db.execute("SELECT audience FROM kh.records WHERE id = %s", (rec,)).fetchone() == ("all",)
    db.commit()


async def test_wrong_from_group(deps, db):
    rec = add_record(db, audience="mgmt")
    db.commit()
    assert await refused(deps, f"rec:{rec}", "all", "mgmt") == errors.MOVE_WRONG_GROUP


async def test_same_group_is_bad_arg(deps, db):
    rec = add_record(db)
    db.commit()
    assert await refused(deps, f"rec:{rec}", "all", "all") == errors.BAD_ARG.format(name="to_group")


async def test_hidden_record_is_not_found(deps, db):
    rec = add_record(db, audience="hr")
    db.commit()
    assert await refused(deps, f"rec:{rec}", "all", "mgmt") == errors.NOT_FOUND
    assert await refused(deps, "rec:00000000-0000-4000-8000-000000000000") == errors.NOT_FOUND


async def test_target_with_same_source_ref_is_conflict(deps, db):
    rec = add_record(db, source="meet", source_ref="same")
    add_record(db, audience="mgmt", source="meet", source_ref="same")
    db.commit()
    assert await refused(deps, f"rec:{rec}") == errors.MOVE_CONFLICT
    assert db.execute("SELECT audience FROM kh.records WHERE id = %s", (rec,)).fetchone() == ("all",)
    db.commit()


async def test_linked_record_is_refused(mcp_client, deps, db):
    rec, other = add_record(db), add_record(db)
    db.commit()
    res = await mcp_client.call(alice_all, "mark_record_outdated",
                                {"id": f"rec:{other}", "replaced_by": f"rec:{rec}"})
    assert not res.is_error, res.text
    assert await refused(deps, f"rec:{rec}") == errors.MOVE_LINKED


async def test_log_line(deps, db, caplog):
    rec = add_record(db)
    db.commit()
    with caplog.at_level(logging.INFO, logger="knowledge_hub.tools"):
        await move(deps, f"rec:{rec}")
    assert f"move_item sub=admin:{ADMIN} id=rec:{rec} from=all to=mgmt" in caplog.text
