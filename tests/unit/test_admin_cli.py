"""``kh label`` and ``kh person`` commands and the dispatcher (plan 45, 3.1)."""

import json

import pytest

from knowledge_hub import cli
from knowledge_hub.admin import apply
from knowledge_hub.admin import cli as admin_cli
from knowledge_hub.setup.steps import Change

pytestmark = pytest.mark.unit

TEXT = """# keep
people:
  alice: a@x.org
  bob: b@x.org
groups:
  all:
    members: [alice, bob]
    approvers: [alice]
  dev:
    members: [alice]
    approvers: [alice]
labels:
  a: first
  b: second
archived_labels: [b]
"""


class FakeLabels:
    def __init__(self, calls):
        self.calls = calls

    async def apply(self, prune):
        self.calls.append(("labels.apply", prune))
        return [Change("+", "label x"), Change("?", "unmanaged label gone")]

    async def plan(self):
        self.calls.append(("labels.plan",))
        return [Change("+", "label x")]

    async def remove_label(self, name, *, write):
        self.calls.append(("remove_label", name, write))
        return Change("-", f"label {name}")


class FakeForgejo:
    def __init__(self, calls):
        self.calls = calls

    async def sync_group(self, name, *, write):
        self.calls.append(("sync_group", name, write))
        return [Change("i", f"group {name}")]


class FakeSteps:
    def __init__(self):
        self.calls = []
        self._labels = FakeLabels(self.calls)
        self._forgejo = FakeForgejo(self.calls)

    def labels(self):
        return self._labels

    def forgejo(self):
        return self._forgejo


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("KH_CONFIG_FILE", raising=False)
    path = tmp_path / "kh.yaml"
    path.write_text(TEXT)
    return path


@pytest.fixture
def steps(monkeypatch):
    fake = FakeSteps()
    monkeypatch.setattr(apply, "default_steps_factory", lambda config: fake)
    return fake


def main(*argv):
    return admin_cli.main(list(argv))


def test_label_add_calls_edit_and_after(cfg, steps, capsys):
    assert main("label", "add", "c", "-d", "third", "--config", str(cfg)) == 0
    assert "  c: third" in cfg.read_text()
    assert steps.calls == [("labels.apply", False)]
    assert "kh.yaml updated" in capsys.readouterr().out


def test_label_add_dry_run_plans(cfg, steps):
    assert main("label", "add", "c", "--dry-run", "--config", str(cfg)) == 0
    assert steps.calls == [("labels.plan",)]
    assert "c:" not in cfg.read_text()


def test_label_remove_targets_the_label(cfg, steps, capsys):
    assert main("label", "remove", "a", "--config", str(cfg)) == 0
    out = capsys.readouterr().out
    assert steps.calls == [("labels.apply", False), ("remove_label", "a", True)]
    assert "- label a" in out
    assert "unmanaged label gone" in out
    assert "  a: first" not in cfg.read_text()


def test_label_remove_refusal_keeps_file(cfg, monkeypatch, capsys):
    class Refusing(FakeSteps):
        def __init__(self):
            super().__init__()
            self._labels.remove_label = self._refuse

        async def _refuse(self, name, *, write):
            return Change("!", f"label {name}: in use")

    monkeypatch.setattr(apply, "default_steps_factory", lambda config: Refusing())
    assert main("label", "remove", "a", "--config", str(cfg)) == 2
    assert cfg.read_text() == TEXT
    assert "kh.yaml unchanged" in capsys.readouterr().out


def test_label_rename_changes_only_description(cfg, capsys):
    assert main("label", "rename", "a", "renamed", "--no-apply", "--config", str(cfg)) == 0
    assert cfg.read_text() == TEXT.replace("a: first", "a: renamed")
    assert "kh.yaml changed; run kh setup apply" in capsys.readouterr().out


def test_label_archive_has_no_apply(cfg, steps):
    assert main("label", "archive", "a", "--config", str(cfg)) == 0
    assert steps.calls == []
    assert "archived_labels: [b, a]" in cfg.read_text()
    assert main("label", "archive", "a", "--config", str(cfg)) == 0  # nothing to change


def test_label_unknown_is_exit_two(cfg, capsys):
    assert main("label", "archive", "zz", "--config", str(cfg)) == 2
    assert "admin: unknown label zz" in capsys.readouterr().out


def test_label_list_output_and_json(cfg, capsys):
    assert main("label", "list", "--config", str(cfg)) == 0
    assert capsys.readouterr().out.splitlines() == [
        "NAME  STATE  DESCRIPTION",
        "a  active  first",
        "b  archived  second",
    ]
    assert main("label", "list", "--json", "--config", str(cfg)) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"name": "a", "description": "first", "archived": False},
        {"name": "b", "description": "second", "archived": True},
    ]


def test_person_add_lowercases_email(cfg, capsys):
    assert main("person", "add", "carol", "Carol@X.ORG", "--config", str(cfg)) == 0
    assert "  carol: carol@x.org" in cfg.read_text()


def test_person_remove_needs_from_groups_when_member(cfg, capsys):
    assert main("person", "remove", "bob", "--config", str(cfg)) == 2
    assert "pass --from-groups" in capsys.readouterr().out
    assert cfg.read_text() == TEXT


def test_person_remove_from_groups_syncs_each_group(cfg, steps, capsys):
    path = cfg
    path.write_text(TEXT.replace("members: [alice]", "members: [alice, bob]"))
    assert main("person", "remove", "bob", "--from-groups", "--config", str(path)) == 0
    assert steps.calls == [("sync_group", "all", True), ("sync_group", "dev", True)]
    assert "bob" not in path.read_text()


def test_person_remove_plain_without_groups(cfg, steps):
    cfg.write_text(TEXT.replace("[alice, bob]", "[alice]"))
    assert main("person", "remove", "bob", "--config", str(cfg)) == 0
    assert steps.calls == []


def test_person_list(cfg, capsys):
    assert main("person", "list", "--config", str(cfg)) == 0
    assert capsys.readouterr().out.splitlines() == [
        "LOGIN  EMAIL  GROUPS",
        "alice  a@x.org  all,dev",
        "bob  b@x.org  all",
    ]
    assert main("person", "list", "--json", "--config", str(cfg)) == 0
    assert json.loads(capsys.readouterr().out)[1] == {"login": "bob", "email": "b@x.org", "groups": ["all"]}


def test_missing_config_is_exit_two(monkeypatch, capsys):
    monkeypatch.delenv("KH_CONFIG_FILE", raising=False)
    assert main("label", "add", "c") == 2
    assert "admin: no config file: pass --config or set KH_CONFIG_FILE" in capsys.readouterr().out
    assert main("label", "list") == 2


def test_config_from_environment(cfg, monkeypatch, capsys):
    monkeypatch.setenv("KH_CONFIG_FILE", str(cfg))
    assert main("label", "archive", "a") == 0
    assert "archived_labels: [b, a]" in cfg.read_text()


def test_argparse_error_is_exit_two():
    with pytest.raises(SystemExit) as exc:
        main("label", "add")
    assert exc.value.code == 2


def test_dispatcher_routes_label_group_person(monkeypatch):
    seen = []
    monkeypatch.setattr(admin_cli, "main", lambda argv: seen.append(argv) or 7)
    for cmd in ("label", "group", "person"):
        assert cli.main([cmd, "list"]) == 7
    assert seen == [["label", "list"], ["group", "list"], ["person", "list"]]


def test_if_sha256_option_parses_and_gates(cfg, steps, capsys):
    assert main("label", "add", "c", "--if-sha256", "0" * 64, "--config", str(cfg)) == 2
    assert "changed since the preview" in capsys.readouterr().out
    digest = apply.text_sha256(TEXT)
    assert main("label", "add", "c", "--if-sha256", digest, "--config", str(cfg)) == 0


def test_if_sha256_rejects_short_value(cfg):
    with pytest.raises(SystemExit) as exc:
        main("label", "add", "c", "--if-sha256", "a" * 63, "--config", str(cfg))
    assert exc.value.code == 2


def test_list_has_no_if_sha256(cfg):
    with pytest.raises(SystemExit) as exc:
        main("label", "list", "--if-sha256", "a" * 64, "--config", str(cfg))
    assert exc.value.code == 2
