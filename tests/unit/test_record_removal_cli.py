"""kh records restore: routing by kh and the checks that run before any config is read."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge_hub import cli
from knowledge_hub.admin import records_cmd

pytestmark = pytest.mark.unit

ADMIN = "admin@example.test"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in [k for k in __import__("os").environ if k.startswith("KH_")]:
        monkeypatch.delenv(k)


def test_records_restore_is_routed_by_kh(monkeypatch, tmp_path, capsys):
    config = tmp_path / "kh.yaml"
    config.write_text(f"admins: [{ADMIN}]\n")
    seen = {}

    async def do_restore(deps, email, audience, source, prefix, since):
        seen.update(email=email, audience=audience, source=source, prefix=prefix, since=since)
        return {"restored": 2}

    async def build_deps(settings, cfg):
        return SimpleNamespace()

    async def close_deps(deps):
        return None

    monkeypatch.setattr(records_cmd, "do_restore", do_restore)
    monkeypatch.setattr(records_cmd, "build_deps", build_deps)
    monkeypatch.setattr(records_cmd, "close_deps", close_deps)
    argv = ["records", "restore", "--audience", "all", "--source", "gdrive", "--prefix", "f1", "--by", ADMIN,
            "--config", str(config)]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == '{"restored": 2}\n'
    assert seen["email"] == ADMIN and seen["audience"] == "all" and seen["source"] == "gdrive"
    assert seen["prefix"] == "f1" and seen["since"] is None


def test_records_restore_since_needs_a_zone(capsys):
    argv = ["records", "restore", "--audience", "all", "--source", "s", "--by", ADMIN,
            "--since", "2026-01-01T00:00:00"]
    assert cli.main(argv) == 2
    assert capsys.readouterr().out.strip() == "records: --since needs a time zone"


def test_records_restore_since_must_parse(capsys):
    argv = ["records", "restore", "--audience", "all", "--source", "s", "--by", ADMIN, "--since", "yesterday"]
    assert cli.main(argv) == 2
    assert capsys.readouterr().out.strip() == "records: --since is not a valid time"
