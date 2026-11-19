"""``kh label`` against a real database and file (plan 45, 3.1)."""

import psycopg
import pytest
import yaml

from knowledge_hub.admin import apply
from knowledge_hub.admin import cli as admin_cli
from knowledge_hub.config import parse_config
from knowledge_hub.setup.labels import LabelsStep
from tests.support.seed import add_doc

pytestmark = pytest.mark.integration

DB = "kh_fresh"
TEXT = """# keep
people:
  alice: a@example.test
groups:
  all:
    members: [alice]
    approvers: [alice]
labels:
  base: Base
"""


class RealSteps:
    def __init__(self, config, dsn):
        self._step = LabelsStep(config, dsn)

    def labels(self):
        return self._step


@pytest.fixture
def cfg(tmp_path, monkeypatch, fresh_db):
    monkeypatch.delenv("KH_CONFIG_FILE", raising=False)
    path = tmp_path / "kh.yaml"
    path.write_text(TEXT)
    dsn = fresh_db.dsn("kh_migrator", DB)
    monkeypatch.setattr(apply, "default_steps_factory", lambda config: RealSteps(config, dsn))
    return path


def names(fresh_db):
    with psycopg.connect(fresh_db.dsn("kh_migrator", DB)) as conn:
        return dict(conn.execute("SELECT name, description FROM kh.labels").fetchall())


def main(*argv):
    return admin_cli.main(list(argv))


def test_label_add_then_rename_then_remove_roundtrip(cfg, fresh_db):
    assert main("label", "add", "ember", "-d", "Ember", "--config", str(cfg)) == 0
    assert parse_config(yaml.safe_load(cfg.read_text())).labels["ember"] == "Ember"
    assert names(fresh_db)["ember"] == "Ember"

    assert main("label", "rename", "ember", "Embers", "--config", str(cfg)) == 0
    assert names(fresh_db)["ember"] == "Embers"
    assert "# keep" in cfg.read_text()

    assert main("label", "remove", "ember", "--config", str(cfg)) == 0
    assert "ember:" not in cfg.read_text()
    assert "ember" not in names(fresh_db)


def test_label_remove_in_use_keeps_the_file(cfg, fresh_db, capsys):
    assert main("label", "add", "ember", "--config", str(cfg)) == 0
    with psycopg.connect(fresh_db.dsn("kh_migrator", DB), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.groups (name, repo) VALUES ('all', 'kb-all') ON CONFLICT DO NOTHING")
        add_doc(conn, labels=["ember"])
    before = cfg.read_text()
    assert main("label", "remove", "ember", "--config", str(cfg)) == 2
    assert cfg.read_text() == before
    assert "kh.yaml unchanged" in capsys.readouterr().out
    assert "ember" in names(fresh_db)
