"""Altering datasets and columns (plan 3.2, p3)."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.config import KhConfig
from knowledge_hub.datasets import cli, ddl
from knowledge_hub.datasets.model import DatasetError
from tests.support.datasets import make_dataset, migrator_dsn

pytestmark = pytest.mark.integration


@pytest.fixture
def deals(seeded_db, fresh_db):
    return make_dataset(fresh_db)


@pytest.fixture
def dsn(fresh_db):
    return migrator_dsn(fresh_db)


def _q(dsn, query, *params):
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def _jobs(dsn):
    q = "SELECT count(*) FROM kh.index_jobs WHERE kind = 'dataset_refresh' AND state = 'queued'"
    return _q(dsn, q)[0][0]


def _clear_jobs(dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute("DELETE FROM kh.index_jobs WHERE kind = 'dataset_refresh'")


def _trigger(dsn):
    return _q(
        dsn,
        "SELECT pg_get_triggerdef(oid) FROM pg_trigger"
        " WHERE tgrelid = 'kh_data.deals'::regclass AND tgname = 'mirror_upd'",
    )[0][0]


def test_rename_title_column(deals, dsn):
    ddl.rename_column(dsn, "deals", "name", "label")
    assert _q(dsn, "SELECT title_column FROM kh.datasets WHERE name = 'deals'")[0][0] == "label"
    assert "OLD.label IS DISTINCT" in _trigger(dsn)
    assert _q(dsn, "SELECT count(*) FROM kh.columns WHERE dataset = 'deals' AND name = 'label'")[0][0] == 1


def test_drop_title_column_refused(deals, dsn):
    with pytest.raises(DatasetError, match="is the title column"):
        ddl.drop_column(dsn, "deals", "name")


def test_drop_embed_column_rebuilds_trigger_and_queues(deals, dsn):
    _clear_jobs(dsn)
    ddl.drop_column(dsn, "deals", "stage")
    assert "stage" not in _trigger(dsn)
    assert _jobs(dsn) == 1
    assert _q(dsn, "SELECT count(*) FROM kh.columns WHERE dataset = 'deals' AND name = 'stage'")[0][0] == 0


def test_sensitive_title_column_refused(deals, dsn):
    with pytest.raises(DatasetError, match="title column cannot be sensitive"):
        ddl.set_flags(dsn, "deals", "name", embed=False, sensitive=True)


def test_embed_and_sensitive_refused(deals, dsn):
    with pytest.raises(DatasetError, match="embed and sensitive cannot both be set"):
        ddl.set_flags(dsn, "deals", "amount", embed=True, sensitive=True)


def _index_count(dsn):
    return _q(
        dsn, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'kh_data' AND tablename = 'deals'"
    )[0][0]


def test_indexed_false_drops_index(deals, dsn):
    before = _index_count(dsn)
    ddl.set_flags(dsn, "deals", "amount", indexed=True)
    assert _index_count(dsn) == before + 1
    ddl.set_flags(dsn, "deals", "amount", indexed=False)
    assert _index_count(dsn) == before


def test_set_choices_outside_stored_value_fails(deals, dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO kh_data.deals (_key, _audience, _source_updated_at, stage)"
            " VALUES ('k', 'all', now(), 'won')"
        )
    with pytest.raises(DatasetError, match="rows hold values outside the new choices"):
        ddl.set_choices(dsn, "deals", "stage", ["open", "lost"])
    ds = ddl.set_choices(dsn, "deals", "stage", ["open", "won", "lost", "stalled"])
    assert ds.column("stage").choices == ("open", "won", "lost", "stalled")


def test_set_dataset_title_column_must_be_text(deals, dsn):
    with pytest.raises(DatasetError, match="title_column must be a text, choice or ref column"):
        ddl.set_dataset(dsn, "deals", title_column="amount")
    with pytest.raises(DatasetError, match="title_column must be"):
        ddl.set_dataset(dsn, "deals", title_column="secret_note")


def test_set_dataset_audiences_queues_refresh(deals, dsn):
    _clear_jobs(dsn)
    ds = ddl.set_dataset(dsn, "deals", audiences=("all",))
    assert ds.audiences == ("all",)
    assert _jobs(dsn) == 1


def test_cli_create_with_admin(deals, dsn, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda path: KhConfig(
            admins=["admin@example.test"], groups={"all": {"repo": "all"}}
        ),
    )

    class S:
        def dsn(self, role):
            return dsn

    monkeypatch.setattr(cli, "load_settings", lambda: S())
    cfg = tmp_path / "kh.yaml"
    cfg.write_text("")
    code = cli.main(
        ["create", "leads", "--title", "Leads", "--audiences", "all", "--by", "Admin@Example.Test",
         "--config", str(cfg)]
    )
    assert capsys.readouterr().out.strip() == "dataset: created leads"
    assert code == 0
    assert _q(dsn, "SELECT to_regclass('kh_data.leads') IS NOT NULL")[0][0]
