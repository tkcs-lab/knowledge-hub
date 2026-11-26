"""The record purge in kh daily, kh records restore, and kh move of a removed record."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from knowledge_hub.admin import records_cmd
from knowledge_hub.admin.move_cmd import do_move
from knowledge_hub.admin.records_cmd import do_restore
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.jobs.daily import purge_records
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import KhError
from knowledge_hub.mcp.server import Deps
from tests.support.bearer import TestBearerResolver
from tests.support.blobs import MemoryBlobStore
from tests.support.pg import FRESH_DB
from tests.support.seed import add_chunk, add_record

pytestmark = pytest.mark.integration

ADMIN = "admin@example.test"
DAYS = 30


@pytest.fixture
def deps(db_settings, kh_config, app_pool, indexer_pool) -> Deps:
    return Deps(
        settings=db_settings.model_copy(update={"env": "test"}), config=kh_config, app_pool=app_pool,
        indexer_pool=indexer_pool, embedder=FakeEmbedder(), bearer=TestBearerResolver(kh_config),
    )


@pytest.fixture
def db(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        yield conn
        conn.commit()


class FailingBlobs(MemoryBlobStore):
    def __init__(self, bad: set[str] | None = None) -> None:
        super().__init__()
        self.bad = bad

    async def delete(self, key: str) -> None:
        if self.bad is None or key in self.bad:
            raise OSError("boom")
        await super().delete(key)


def ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def removed(db, days: float, reason: str = "delete", **fields) -> str:
    rec = add_record(
        db, outdated=True, outdated_at=ago(days), outdated_by="kh-bot", outdated_reason=reason, **fields
    )
    db.commit()
    return rec


def exists(db, rec: str) -> bool:
    row = db.execute("SELECT count(*) FROM kh.records WHERE id = %s", (rec,)).fetchone()
    db.commit()
    return row[0] == 1


def reason(db, rec: str) -> str:
    row = db.execute("SELECT outdated_reason FROM kh.records WHERE id = %s", (rec,)).fetchone()
    db.commit()
    return row[0]


async def test_purge_records_deletes_row_chunks_and_blob(db, indexer_pool):
    rec = removed(db, 31, blob_key="records/gone")
    add_chunk(db, record_id=rec, tier="T3")
    db.commit()
    blobs = MemoryBlobStore()
    await blobs.put("records/gone", b"body", "text/plain")
    assert await purge_records(indexer_pool, blobs, DAYS) == (1, 0)
    assert not exists(db, rec)
    assert db.execute("SELECT count(*) FROM kh.chunks WHERE record_id = %s", (rec,)).fetchone() == (0,)
    db.commit()
    assert blobs.objects == {}


async def test_purge_records_keeps_recent_and_manual_marks(db, indexer_pool):
    recent = removed(db, 2)
    manual = removed(db, 100, reason="")
    assert await purge_records(indexer_pool, MemoryBlobStore(), DAYS) == (0, 0)
    assert exists(db, recent) and exists(db, manual)


async def test_purge_records_keeps_the_row_when_the_blob_fails(db, indexer_pool):
    rec = removed(db, 31, blob_key="records/stuck")
    assert await purge_records(indexer_pool, FailingBlobs(), DAYS) == (0, 1)
    assert exists(db, rec) and reason(db, rec) == "purge"
    assert await purge_records(indexer_pool, MemoryBlobStore(), DAYS) == (1, 0)
    assert not exists(db, rec)


async def test_purge_records_counts_a_failed_blob_once(db, indexer_pool):
    good = removed(db, 31, blob_key="records/good")
    bad = removed(db, 32, blob_key="records/bad")
    assert await purge_records(indexer_pool, FailingBlobs({"records/bad"}), DAYS) == (1, 1)
    assert not exists(db, good) and exists(db, bad)


async def test_purge_records_ignores_dataset_rows(db, indexer_pool):
    rec = add_record(
        db, source="dataset:deals", dataset_row="row:deals:1", outdated=True, outdated_at=ago(100),
        outdated_by="kh-bot",
    )
    db.commit()
    assert await purge_records(indexer_pool, MemoryBlobStore(), DAYS) == (0, 0)
    assert exists(db, rec)


async def test_restore_record_command_restores_by_prefix_and_since(deps, db):
    old = removed(db, 5, source="gdrive", source_ref="f1@a")
    new_a = removed(db, 1, source="gdrive", source_ref="f1@b")
    other = removed(db, 1, source="gdrive", source_ref="f2@a")
    purging = removed(db, 1, reason="purge", source="gdrive", source_ref="f1@c")
    res = await do_restore(deps, ADMIN, "all", "gdrive", "f1", ago(2))
    assert res == {"restored": 1}
    assert reason(db, new_a) == "" and reason(db, old) == "delete"
    assert reason(db, other) == "delete" and reason(db, purging) == "purge"
    row = db.execute("SELECT outdated FROM kh.records WHERE id = %s", (new_a,)).fetchone()
    db.commit()
    assert row == (False,)
    assert await do_restore(deps, ADMIN, "all", "gdrive", None, None) == {"restored": 2}


async def test_restore_refuses_a_non_administrator_and_a_bad_group(tmp_path, capsys):
    config = tmp_path / "kh.yaml"
    config.write_text(f"admins: [{ADMIN}]\n")

    def args(by: str, audience: str) -> argparse.Namespace:
        return argparse.Namespace(
            config=config, by=by, audience=audience, source="gdrive", prefix=None, since=None
        )

    assert await records_cmd._run(args("nobody@example.test", "all")) == 1
    assert capsys.readouterr().out.strip() == "records: nobody@example.test is not an administrator"
    assert await records_cmd._run(args(ADMIN, "Bad Group!")) == 2
    assert capsys.readouterr().out.strip() == "records: group name is not valid"


async def test_move_refuses_a_removed_record(deps, db):
    rec = removed(db, 1)
    with pytest.raises(KhError) as exc:
        await do_move(deps, ADMIN, f"rec:{rec}", "all", "mgmt")
    assert exc.value.text == errors.MOVE_REMOVED
