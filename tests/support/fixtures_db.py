"""Database fixtures: one cluster per session, one fresh database per test."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.db.migrate import migrate
from tests.support.pg import FRESH_DB, TEMPLATE_DB, Cluster, bootstrap_roles


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
