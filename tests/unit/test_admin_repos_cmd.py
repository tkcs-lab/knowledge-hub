"""``kh repo`` commands with fake steps."""

import json

import pytest

from knowledge_hub.admin import apply
from knowledge_hub.admin import cli as admin_cli
from knowledge_hub.setup.steps import Change

pytestmark = pytest.mark.unit

BASE = """# keep
people:
  alice: a@x.org
groups:
  all:
    members: [alice]
    approvers: [alice]
labels:
  product:ember: Ember
"""
TECH = BASE + (
    "tech_org: kh-tech\ntech_repos:\n  core: {}\n  ember-core:\n"
    "    labels: [product:ember]\n    dirs: [docs, adr]\n"
)


class FakeForgejo:
    def __init__(self, calls, missing=False):
        self.calls, self.missing = calls, missing

    async def sync_tech_repo(self, name, *, write):
        self.calls.append(("sync_tech_repo", name, write))
        if self.missing:
            return [Change("!", f"tech repo {name}: not in org kh-tech; create the clone or fork there")]
        return [Change("i", f"tech repo {name}: collaborators none")]


class FakeTech:
    def __init__(self, calls, usage):
        self.calls, self.usage = calls, usage

    async def tech_usage(self, name):
        self.calls.append(("tech_usage", name))
        return self.usage

    async def remove_repo(self, name, *, write):
        self.calls.append(("remove_repo", name, write))
        return Change("-", f"tech documents of {name}")


class FakeSteps:
    def __init__(self, usage=0, missing=False):
        self.calls = []
        self._fj = FakeForgejo(self.calls, missing)
        self._tech = FakeTech(self.calls, usage)

    def forgejo(self):
        return self._fj

    def tech(self):
        return self._tech


def write(tmp_path, monkeypatch, text):
    monkeypatch.delenv("KH_CONFIG_FILE", raising=False)
    path = tmp_path / "kh.yaml"
    path.write_text(text)
    return path


def use(monkeypatch, **kw):
    fake = FakeSteps(**kw)
    monkeypatch.setattr(apply, "default_steps_factory", lambda config: fake)
    return fake


def main(*argv):
    return admin_cli.main(list(argv))


def test_list_text_and_json(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, monkeypatch, TECH)
    assert main("repo", "list", "--config", str(cfg)) == 0
    assert capsys.readouterr().out.splitlines() == [
        "NAME  DIRS  LABELS",
        "core  docs  -",
        "ember-core  docs,adr  product:ember",
    ]
    assert main("repo", "list", "--json", "--config", str(cfg)) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"name": "core", "dirs": ["docs"], "labels": []},
        {"name": "ember-core", "dirs": ["docs", "adr"], "labels": ["product:ember"]},
    ]


def test_add_edits_and_syncs_that_repo(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, monkeypatch, TECH)
    steps = use(monkeypatch)
    args = ["repo", "add", "new", "--dirs", "docs,adr", "--labels", "product:ember", "--config", str(cfg)]
    assert main(*args) == 0
    assert "  new:\n    labels: [product:ember]\n    dirs: [docs, adr]\n" in cfg.read_text()
    assert steps.calls == [("sync_tech_repo", "new", True)]
    assert "kh.yaml updated" in capsys.readouterr().out


def test_add_without_flags_writes_an_empty_entry(tmp_path, monkeypatch):
    cfg = write(tmp_path, monkeypatch, TECH)
    use(monkeypatch)
    assert main("repo", "add", "plain", "--config", str(cfg)) == 0
    assert cfg.read_text().endswith("  plain: {}\n")


def test_add_without_tech_org_is_refused(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, monkeypatch, BASE)
    use(monkeypatch)
    assert main("repo", "add", "x", "--config", str(cfg)) == 2
    assert "admin: tech_org is not set; add it to kh.yaml first" in capsys.readouterr().out
    assert cfg.read_text() == BASE


def test_add_empty_dirs_fails_the_config_check(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, monkeypatch, TECH)
    use(monkeypatch)
    assert main("repo", "add", "x", "--dirs", "", "--config", str(cfg)) == 2
    assert "config: tech repo x needs at least one dir" in capsys.readouterr().out
    assert cfg.read_text() == TECH


def test_add_refused_by_the_step_leaves_the_file(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, monkeypatch, TECH)
    use(monkeypatch, missing=True)
    assert main("repo", "add", "gone", "--config", str(cfg)) == 2
    out = capsys.readouterr().out
    assert "! tech repo gone: not in org kh-tech; create the clone or fork there" in out
    assert "kh.yaml unchanged" in out
    assert cfg.read_text() == TECH


def test_set_edits_only_and_prints_the_rebuild_note(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, monkeypatch, TECH)
    steps = use(monkeypatch)
    assert main("repo", "set", "ember-core", "--dirs", "docs", "--labels", "", "--config", str(cfg)) == 0
    text = cfg.read_text()
    assert "    labels: []\n    dirs: [docs]\n" in text
    assert steps.calls == []
    assert "kh rebuild --repo ember-core" in capsys.readouterr().out


def test_set_on_a_flow_entry(tmp_path, monkeypatch):
    cfg = write(tmp_path, monkeypatch, TECH)
    use(monkeypatch)
    assert main("repo", "set", "core", "--dirs", "adr", "--config", str(cfg)) == 0
    assert "  core:\n    dirs: [adr]\n" in cfg.read_text()


def test_set_without_flags_is_refused(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, monkeypatch, TECH)
    assert main("repo", "set", "core", "--config", str(cfg)) == 2
    assert "admin: nothing to set: pass --dirs or --labels" in capsys.readouterr().out


def test_remove_with_documents_needs_yes(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, monkeypatch, TECH)
    steps = use(monkeypatch, usage=7)
    assert main("repo", "remove", "core", "--config", str(cfg)) == 2
    assert (
        "admin: tech repo core: removing it deletes 7 indexed documents; pass --yes to continue"
        in capsys.readouterr().out
    )
    assert cfg.read_text() == TECH
    assert steps.calls == [("tech_usage", "core")]


def test_remove_yes_deletes_documents_and_keeps_the_other_repos(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, monkeypatch, TECH)
    steps = use(monkeypatch, usage=7)
    assert main("repo", "remove", "core", "--yes", "--config", str(cfg)) == 0
    text = cfg.read_text()
    assert "core: {}" not in text and "ember-core:" in text
    assert steps.calls == [("remove_repo", "core", True)]
    out = capsys.readouterr().out
    assert "the Forgejo repo core in the tech org is kept" in out
    assert "restart kh serve now: until then its worker still indexes core" in out


def test_remove_with_no_documents_needs_no_yes(tmp_path, monkeypatch):
    cfg = write(tmp_path, monkeypatch, TECH)
    steps = use(monkeypatch, usage=0)
    assert main("repo", "remove", "core", "--config", str(cfg)) == 0
    assert steps.calls == [("tech_usage", "core"), ("remove_repo", "core", True)]


def test_remove_no_apply_needs_no_yes_and_runs_no_step(tmp_path, monkeypatch):
    cfg = write(tmp_path, monkeypatch, TECH)
    steps = use(monkeypatch, usage=7)
    assert main("repo", "remove", "core", "--no-apply", "--config", str(cfg)) == 0
    assert steps.calls == []
    assert "core: {}" not in cfg.read_text()


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    cfg = write(tmp_path, monkeypatch, TECH)
    steps = use(monkeypatch, usage=7)
    assert main("repo", "add", "n", "--dry-run", "--config", str(cfg)) == 0
    assert main("repo", "set", "core", "--dirs", "adr", "--dry-run", "--config", str(cfg)) == 0
    assert main("repo", "remove", "core", "--yes", "--dry-run", "--config", str(cfg)) == 0
    assert cfg.read_text() == TECH
    assert steps.calls == [("sync_tech_repo", "n", False), ("remove_repo", "core", False)]
