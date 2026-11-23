"""kh dataset: parsing, exit codes and output (no database)."""

from __future__ import annotations

import psycopg
import pytest

from knowledge_hub.config import KhConfig
from knowledge_hub.datasets import cli, ddl

pytestmark = pytest.mark.unit

CASES = [
    ["create", "deals", "--title", "Deals", "--audiences", "all,mgmt", "--by", "a@x.test"],
    ["add-column", "deals", "name", "--type", "text", "--embed", "--by", "a@x.test"],
    ["rename-column", "deals", "a", "b", "--by", "a@x.test"],
    ["drop-column", "deals", "a", "--yes", "--by", "a@x.test"],
    ["set-flags", "deals", "a", "--no-embed", "--filterable", "--label", "L", "--by", "a@x.test"],
    ["set-choices", "deals", "stage", "a,b,c", "--by", "a@x.test"],
    ["set", "deals", "--title", "T", "--audiences", "all", "--max-outdated-percent", "5", "--by", "a@x.test"],
    ["list", "--json"],
    ["show", "deals"],
]


@pytest.mark.parametrize("argv", CASES, ids=[c[0] for c in CASES])
def test_every_subcommand_parses(argv):
    assert cli.build_parser().parse_args(argv).command == argv[0]


@pytest.fixture
def admin(monkeypatch, tmp_path):
    cfg = tmp_path / "kh.yaml"
    cfg.write_text("")
    monkeypatch.setattr(cli, "load_config", lambda path: KhConfig(admins=["a@x.test"]))

    class S:
        def dsn(self, role):
            return "dsn"

    monkeypatch.setattr(cli, "load_settings", lambda: S())
    return ["--config", str(cfg)]


def test_drop_column_needs_yes(admin, capsys):
    assert cli.main(["drop-column", "deals", "amount", "--by", "a@x.test", *admin]) == 2
    assert capsys.readouterr().out.strip() == "dataset: add --yes to drop column amount and its values"


def test_non_admin_refused(admin, capsys):
    assert cli.main(["drop-column", "deals", "amount", "--yes", "--by", "eve@x.test", *admin]) == 1
    assert capsys.readouterr().out.strip() == "dataset: eve@x.test is not an administrator"


def test_database_error(admin, monkeypatch, capsys):
    def boom(*a, **k):
        raise psycopg.OperationalError("connection refused\nmore")

    monkeypatch.setattr(ddl, "drop_column", boom)
    assert cli.main(["drop-column", "deals", "amount", "--yes", "--by", "A@x.test", *admin]) == 1
    assert capsys.readouterr().out.strip() == "dataset: database error: connection refused"
