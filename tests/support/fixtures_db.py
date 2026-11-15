"""Database fixtures: one cluster per session, one fresh database per test."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.db.migrate import migrate
from knowledge_hub.db.pool import make_app_pool, make_indexer_pool
from knowledge_hub.settings import Settings
from tests.support.pg import FRESH_DB, TEMPLATE_DB, Cluster, bootstrap_roles
from tests.support.seed import seed_rows


@pytest.fixture(scope="session")
def pg_cluster():
    cluster = Cluster()
    cluster.start()
    try:
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(scope="session")
def migrated_db(pg_cluster):
    bootstrap_roles(pg_cluster)
    migrate(pg_cluster.dsn("kh_migrator"))
    with psycopg.connect(pg_cluster.admin_dsn(), autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {TEMPLATE_DB} TEMPLATE kh OWNER kh_migrator")
    return pg_cluster


@pytest.fixture
def fresh_db(migrated_db):
    """Return the cluster; database ``kh_fresh`` is recreated from the migrated template."""
    with psycopg.connect(migrated_db.admin_dsn(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {FRESH_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {FRESH_DB} TEMPLATE {TEMPLATE_DB} OWNER kh_migrator")
    return migrated_db


@pytest.fixture
def db_settings(fresh_db):
    return Settings(db_host=str(fresh_db.sock), db_port=5432, db_name=FRESH_DB)


@pytest.fixture
async def app_pool(db_settings):
    pool = make_app_pool(db_settings)
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def indexer_pool(db_settings):
    pool = make_indexer_pool(db_settings)
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def seeded_db(fresh_db):
    """Fresh database plus the leak-test seed; returns ``{table: {case: id}}``."""
    return seed_rows(fresh_db.admin_dsn(FRESH_DB))
