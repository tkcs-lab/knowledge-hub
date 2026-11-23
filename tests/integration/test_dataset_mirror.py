"""One T3 record per dataset row: mirror function, refresh job, embedding and search hits (plan 3.4)."""

from __future__ import annotations

import time

import psycopg
import pytest

from knowledge_hub.datasets import ddl
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.worker import Worker
from knowledge_hub.mcp.server import Deps
from tests.support.bearer import TestBearerResolver
from tests.support.datasets import make_dataset, migrator_dsn
from tests.support.pg import FRESH_DB
from tests.support.principals import KH_CONFIG, alice_all

pytestmark = pytest.mark.integration


@pytest.fixture
def app_settings(db_settings, tmp_path):
    from tests.support.mcp_client import HOST

    key = tmp_path / "cursor.key"
    key.write_bytes(b"k" * 32)
    return db_settings.model_copy(update={"env": "test", "allowed_hosts": HOST, "cursor_key_file": key})


@pytest.fixture
def deps(app_settings, kh_config, app_pool, indexer_pool) -> Deps:
    return Deps(
        settings=app_settings, config=kh_config, app_pool=app_pool, indexer_pool=indexer_pool,
        embedder=FakeEmbedder(), bearer=TestBearerResolver(kh_config),
    )


@pytest.fixture
def world(seeded_db, fresh_db):
    make_dataset(fresh_db)
    drain_jobs(fresh_db)  # the seed rows queued jobs of their own
    return fresh_db


def mig(world):
    return psycopg.connect(migrator_dsn(world), autocommit=True)


def add_row(world, key="k1", audience="all", name="Acme deal", **extra):
    cols = {"name": name, "stage": "open", "amount": 10, "secret_note": "hunter2", **extra}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with mig(world) as conn:
        return str(conn.execute(
            f"INSERT INTO kh_data.deals (_key, _audience, _source_updated_at, {names})"
            f" VALUES (%s, %s, now(), {marks}) RETURNING _id",
            (key, audience, *cols.values()),
        ).fetchone()[0])


def record(world, rid):
    with mig(world) as conn:
        return conn.execute(
            "SELECT title, excerpt, source, blob_key, outdated, audience, dataset_row"
            " FROM kh.records WHERE dataset_row = %s", (f"row:deals:{rid}",)
        ).fetchall()


def embed_jobs(world):
    with mig(world) as conn:
        return conn.execute(
            "SELECT count(*) FROM kh.index_jobs WHERE kind = 'embed_record' AND state = 'queued'"
        ).fetchone()[0]


def drain_jobs(world):
    with mig(world) as conn:
        conn.execute("DELETE FROM kh.index_jobs")


def make_worker(indexer_pool):
    return Worker(
        indexer_pool, None, FakeEmbedder(1024), KH_CONFIG, None, chunker_version=1, poll_seconds=0.05
    )


def test_insert_creates_one_record(world):
    rid = add_row(world)
    [(title, excerpt, source, blob_key, outdated, aud, drow)] = record(world, rid)
    assert (title, source, blob_key, outdated, aud, drow) == (
        "Acme deal", "dataset:deals", "", False, "all", f"row:deals:{rid}")
    assert excerpt.splitlines()[0] == "Deals: Acme deal"
    assert "Stage: open" in excerpt and "hunter2" not in excerpt and "amount" not in excerpt.lower()


def test_update_of_unembedded_column_changes_nothing(world):
    rid = add_row(world)
    drain_jobs(world)
    with mig(world) as conn:
        conn.execute("UPDATE kh_data.deals SET amount = 99 WHERE _id = %s", (rid,))
    assert embed_jobs(world) == 0 and len(record(world, rid)) == 1


def test_update_of_name_changes_title_and_queues_one_job(world):
    rid = add_row(world)
    drain_jobs(world)
    with mig(world) as conn:
        conn.execute("UPDATE kh_data.deals SET name = 'Zed deal' WHERE _id = %s", (rid,))
        assert embed_jobs(world) == 1
        conn.execute("UPDATE kh_data.deals SET name = 'Zed deal 2' WHERE _id = %s", (rid,))
    [(title, excerpt, *_)] = record(world, rid)
    assert title == "Zed deal 2" and excerpt.startswith("Deals: Zed deal 2")
    assert embed_jobs(world) == 1


def test_outdated_flag_follows_the_row(world):
    rid = add_row(world)
    with mig(world) as conn:
        conn.execute(
            "UPDATE kh_data.deals SET _outdated = true, _outdated_reason = 'delete', _outdated_at = now()"
            " WHERE _id = %s", (rid,))
        sql = "SELECT outdated, outdated_by, outdated_at FROM kh.records WHERE dataset_row <> ''"
        assert conn.execute(sql).fetchone()[:2] == (True, "dataset")
        conn.execute(
            "UPDATE kh_data.deals SET _outdated = false, _outdated_reason = '', _outdated_at = NULL"
            " WHERE _id = %s", (rid,))
        assert conn.execute(sql).fetchone() == (False, "", None)


def test_refresh_deletes_records_of_a_removed_audience(world):
    keep = add_row(world, "k1", "all")
    gone = add_row(world, "k2", "all", name="Other")
    with mig(world) as conn:
        conn.execute("UPDATE kh_data.deals SET _audience = _audience")  # no-op update, no mirror
    ddl_dsn = migrator_dsn(world)
    with psycopg.connect(ddl_dsn) as conn:
        conn.execute("UPDATE kh.datasets SET audiences = '{mgmt}' WHERE name = 'deals'")
    with psycopg.connect(world.dsn("kh_indexer", FRESH_DB), autocommit=True) as conn:
        after = None
        while True:
            after = conn.execute("SELECT kh.dataset_refresh('deals', %s, 1000)", (after,)).fetchone()[0]
            if after is None:
                break
    assert record(world, keep) == [] and record(world, gone) == []


def test_set_dataset_audiences_queues_a_refresh(world):
    ddl.set_dataset(migrator_dsn(world), "deals", audiences=("mgmt",))
    with mig(world) as conn:
        assert conn.execute(
            "SELECT count(*) FROM kh.index_jobs WHERE kind = 'dataset_refresh'").fetchone()[0] >= 1


async def test_embed_record_of_a_row_needs_no_blob_store(world, indexer_pool):
    rid = add_row(world)
    await make_worker(indexer_pool).run_once()
    with mig(world) as conn:
        rec = conn.execute(
            "SELECT id FROM kh.records WHERE dataset_row = %s", (f"row:deals:{rid}",)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT tier, audience, embedding IS NOT NULL, text FROM kh.chunks WHERE record_id = %s",
            (rec,),
        ).fetchall()
    assert rows and all(r[:3] == ("T3", "all", True) for r in rows)
    assert "Acme deal" in rows[0][3] and "hunter2" not in rows[0][3]


async def test_search_returns_the_row_hit(world, mcp_client, indexer_pool):
    rid = add_row(world, name="Quokka renewal")
    worker = make_worker(indexer_pool)
    while await worker.run_once():
        pass
    res = await mcp_client.call(alice_all, "search", {"query": "Quokka renewal", "min_tier": "T3"})
    assert not res.is_error, res.text
    hit = next(h for h in res.data["hits"] if h["row"])
    assert hit["row"] == f"row:deals:{rid}" and hit["tier"] == "T3" and hit["title"] == "Quokka renewal"
    assert all(h["row"] == "" for h in res.data["hits"] if h is not hit)


def test_bulk_insert_stays_fast(world):
    with mig(world) as conn:
        start = time.monotonic()
        conn.execute(
            "INSERT INTO kh_data.deals (_key, _audience, _source_updated_at, name, stage)"
            " SELECT 'k' || g, 'all', now(), 'Deal ' || g, 'won' FROM generate_series(1, 500) g")
        elapsed = time.monotonic() - start
        assert conn.execute("SELECT count(*) FROM kh.records WHERE dataset_row <> ''").fetchone()[0] == 500
    assert elapsed < 2, elapsed
