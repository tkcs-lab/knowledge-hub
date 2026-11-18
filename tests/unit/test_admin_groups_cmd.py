"""``kh group`` commands with fake steps (plan 45, 3.1, 3.7, 3.8)."""

import json
import re

import pytest

from knowledge_hub.admin import apply
from knowledge_hub.admin import cli as admin_cli
from knowledge_hub.setup.steps import Change

pytestmark = pytest.mark.unit

TEXT = """# keep
people:
  alice: a@x.org
  bob: b@x.org
  carol: c@x.org
groups:
  all:
    members: [alice, bob]
    approvers: [alice]
  dev:
    repo: kb-dev
    members: [alice, bob]
    approvers: [alice]
    slack_channel: "#dev"
"""

TECH = TEXT + "tech_org: kh-tech\ntech_repos:\n  core: {}\n"


class FakePostgres:
    def __init__(self, calls, usage=(3, 0), refuse=False):
        self.calls, self.usage, self.refuse = calls, usage, refuse

    async def sync_groups(self, *, write):
        self.calls.append(("sync_groups", write))
        return [Change("+", "group x")]

    async def remove_group(self, name, *, write):
        self.calls.append(("pg.remove_group", name, write))
        if self.refuse:
            return Change("!", f"group {name}: still has 2 records")
        return Change("-", f"group {name}")

    async def group_usage(self, name):
        self.calls.append(("group_usage", name))
        return self.usage


class FakeForgejo:
    def __init__(self, calls, missing=(), slack_missing=False):
        self.calls, self.missing, self.slack_missing = calls, set(missing), slack_missing
        self.teams = {"dev", "dev-approve"}

    async def sync_group(self, name, *, write):
        self.calls.append(("sync_group", name, write))
        out = [Change("i", f"group {name}")]
        if self.slack_missing:
            out.append(Change("!", f"slack hook kb-{name}: no URL for group {name}"))
        return out

    async def remove_group(self, name, *, repo, write):
        self.calls.append(("fj.remove_group", name, repo, write))
        if write:
            self.teams -= {name, f"{name}-approve"}
        return [Change("-", f"team {name}"), Change("i", f"repo {repo}: kept")]

    async def missing_accounts(self, logins):
        self.calls.append(("missing_accounts", list(logins)))
        return [x for x in logins if x in self.missing]


class FakeSteps:
    def __init__(self, **kw):
        self.calls = []
        self._pg = FakePostgres(self.calls, **{k: v for k, v in kw.items() if k in ("usage", "refuse")})
        self._fj = FakeForgejo(
            self.calls, **{k: v for k, v in kw.items() if k in ("missing", "slack_missing")}
        )

    def postgres(self):
        return self._pg

    def forgejo(self):
        return self._fj


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("KH_CONFIG_FILE", raising=False)
    path = tmp_path / "kh.yaml"
    path.write_text(TEXT)
    return path


def use(monkeypatch, **kw):
    fake = FakeSteps(**kw)
    monkeypatch.setattr(apply, "default_steps_factory", lambda config: fake)
    return fake


def main(*argv):
    return admin_cli.main(list(argv))


def test_group_add_edits_and_syncs(cfg, monkeypatch, capsys):
    steps = use(monkeypatch)
    assert (
        main("group", "add", "ops", "--members", "alice,bob", "--approvers", "alice", "--config", str(cfg))
        == 0
    )
    text = cfg.read_text()
    assert "  ops:\n    members: [alice, bob]\n    approvers: [alice]\n" in text
    assert steps.calls == [
        ("missing_accounts", ["alice", "bob"]),
        ("sync_groups", True),
        ("sync_group", "ops", True),
    ]
    out = capsys.readouterr().out
    assert "a token creator must be a member of the group in kh.yaml" in out
    assert "kh.yaml updated" in out


def test_group_add_with_slack_channel(cfg, monkeypatch, capsys):
    use(monkeypatch)
    args = ["group", "add", "ops", "--members", "alice", "--approvers", "alice", "--repo", "kb-ops"]
    assert main(*args, "--slack-channel", "#ops", "--config", str(cfg)) == 0
    assert cfg.read_text().endswith(
        "  ops:\n    repo: kb-ops\n    members: [alice]\n    approvers: [alice]\n    slack_channel: '#ops'\n"
    )
    assert (
        main(
            "group",
            "add",
            "x",
            "--members",
            "alice",
            "--approvers",
            "alice",
            "--slack-channel",
            "bad",
            "--config",
            str(cfg),
        )
        == 2
    )
    assert "config:" in capsys.readouterr().out


def test_member_add_on_slack_group_without_url_exits_two_but_commits(cfg, monkeypatch, capsys):
    use(monkeypatch, slack_missing=True)
    assert main("group", "member", "add", "dev", "carol", "--config", str(cfg)) == 2
    out = capsys.readouterr().out
    assert "! slack hook kb-dev: no URL for group dev" in out
    assert "kh.yaml changed; 1 lines need your attention" in out
    assert "carol" in cfg.read_text().split("dev:")[1]


def test_group_remove_of_all_with_tech_repos_is_refused_by_validation(cfg, monkeypatch, capsys):
    cfg.write_text(TECH)
    steps = use(monkeypatch)
    before = cfg.read_text()
    assert main("group", "remove", "all", "--yes", "--config", str(cfg)) == 2
    assert "config: tech_repos needs the group all" in capsys.readouterr().out
    assert cfg.read_text() == before
    assert steps.calls == []


def test_group_add_refuses_missing_account(cfg, monkeypatch, capsys):
    steps = use(monkeypatch, missing={"carol"})
    args = ["group", "add", "ops", "--members", "alice,carol", "--approvers", "alice", "--config", str(cfg)]
    assert main(*args) == 2
    assert "admin: account carol does not exist in Forgejo; create it first" in capsys.readouterr().out
    assert cfg.read_text() == TEXT
    assert ("sync_groups", True) not in steps.calls


def test_group_remove_needs_yes_and_shows_counts(cfg, monkeypatch, capsys):
    steps = use(monkeypatch, usage=(4, 0))
    assert main("group", "remove", "dev", "--config", str(cfg)) == 2
    out = capsys.readouterr().out
    assert (
        "admin: group dev: removing it deletes 4 documents from the index and keeps the Forgejo repo "
        "kb-dev; pass --yes to continue" in out
    )
    assert cfg.read_text() == TEXT
    assert steps.calls == [("group_usage", "dev")]
    steps2 = use(monkeypatch, usage=(4, 5))
    assert main("group", "remove", "dev", "--dry-run", "--config", str(cfg)) == 2
    assert "admin: group dev still has 5 records" in capsys.readouterr().out
    assert steps2.calls == [("group_usage", "dev")]


def test_group_remove_no_apply_needs_yes(cfg, monkeypatch, capsys):
    steps = use(monkeypatch)
    assert main("group", "remove", "dev", "--no-apply", "--config", str(cfg)) == 2
    assert "--yes is required" in capsys.readouterr().out
    assert cfg.read_text() == TEXT
    assert steps.calls == []
    assert main("group", "remove", "dev", "--no-apply", "--dry-run", "--config", str(cfg)) == 0
    assert cfg.read_text() == TEXT
    assert main("group", "remove", "dev", "--no-apply", "--yes", "--config", str(cfg)) == 0
    assert "dev:" not in cfg.read_text()
    assert steps.calls == []


def test_group_remove_with_yes_removes_postgres_then_teams(cfg, monkeypatch, capsys):
    steps = use(monkeypatch)
    assert main("group", "remove", "dev", "--yes", "--config", str(cfg)) == 0
    assert steps.calls == [("pg.remove_group", "dev", True), ("fj.remove_group", "dev", "kb-dev", True)]
    assert "dev:" not in cfg.read_text()
    assert (
        "the Forgejo repo kb-dev is kept; delete it in Forgejo if you want it gone" in capsys.readouterr().out
    )


def test_group_remove_refused_keeps_the_file_and_the_teams(cfg, monkeypatch, capsys):
    steps = use(monkeypatch, refuse=True)
    assert main("group", "remove", "dev", "--yes", "--config", str(cfg)) == 2
    assert cfg.read_text() == TEXT
    assert steps._fj.teams == {"dev", "dev-approve"}
    assert steps.calls == [("pg.remove_group", "dev", True)]
    assert "kh.yaml unchanged" in capsys.readouterr().out


def test_group_archive_and_unarchive_toggle_the_key(cfg, monkeypatch):
    steps = use(monkeypatch)
    assert main("group", "archive", "dev", "--config", str(cfg)) == 0
    assert "    archived: true\n" in cfg.read_text()
    assert main("group", "archive", "dev", "--config", str(cfg)) == 0  # nothing to change
    assert main("group", "unarchive", "dev", "--config", str(cfg)) == 0
    assert cfg.read_text() == TEXT
    assert steps.calls == [("sync_group", "dev", True), ("sync_group", "dev", True)]


def test_unarchive_prints_rebuild_note(cfg, monkeypatch, capsys):
    use(monkeypatch)
    assert main("group", "archive", "dev", "--no-apply", "--config", str(cfg)) == 0
    capsys.readouterr()
    assert main("group", "unarchive", "dev", "--config", str(cfg)) == 0
    assert "run kh rebuild --repo kb-dev" in capsys.readouterr().out


def test_member_add_unknown_person(cfg, monkeypatch, capsys):
    use(monkeypatch)
    assert main("group", "member", "add", "dev", "zed", "--config", str(cfg)) == 2
    assert "admin: login zed is not in people; run kh person add first" in capsys.readouterr().out
    assert main("group", "member", "add", "nope", "carol", "--config", str(cfg)) == 2
    assert "admin: unknown group nope" in capsys.readouterr().out


def test_member_add_and_remove_sync_the_group(cfg, monkeypatch):
    steps = use(monkeypatch)
    assert main("group", "member", "add", "dev", "carol", "--config", str(cfg)) == 0
    assert "members: [alice, bob, carol]" in cfg.read_text()
    assert main("group", "member", "remove", "dev", "carol", "--config", str(cfg)) == 0
    assert cfg.read_text() == TEXT
    assert steps.calls == [
        ("missing_accounts", ["carol"]),
        ("sync_group", "dev", True),
        ("sync_group", "dev", True),
    ]


def test_member_remove_of_approver_needs_flag(cfg, monkeypatch, capsys):
    cfg.write_text(TEXT.replace("approvers: [alice]", "approvers: [alice, bob]", 2))
    before = cfg.read_text()
    steps = use(monkeypatch)
    assert main("group", "member", "remove", "dev", "bob", "--config", str(cfg)) == 2
    assert "pass --with-approver" in capsys.readouterr().out
    assert cfg.read_text() == before
    assert main("group", "member", "remove", "dev", "bob", "--with-approver", "--config", str(cfg)) == 0
    assert steps.calls == [("sync_group", "dev", True)]
    assert "bob" not in cfg.read_text().split("dev:")[1]


def test_approver_remove_last_approver(cfg, monkeypatch, capsys):
    use(monkeypatch)
    assert main("group", "approver", "remove", "dev", "alice", "--config", str(cfg)) == 2
    assert "admin: group dev needs at least one approver" in capsys.readouterr().out
    assert main("group", "approver", "add", "dev", "bob", "--config", str(cfg)) == 0
    assert "approvers: [alice, bob]" in cfg.read_text()
    assert main("group", "approver", "remove", "dev", "alice", "--config", str(cfg)) == 0


def test_group_list_output_and_json(cfg, capsys):
    assert main("group", "list", "--config", str(cfg)) == 0
    assert capsys.readouterr().out.splitlines() == [
        "NAME  REPO  STATE  MEMBERS  APPROVERS  SLACK",
        "all  kb-all  active  alice,bob  alice  -",
        "dev  kb-dev  active  alice,bob  alice  #dev",
    ]
    assert main("group", "list", "--json", "--config", str(cfg)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[1] == {
        "name": "dev",
        "repo": "kb-dev",
        "archived": False,
        "members": ["alice", "bob"],
        "approvers": ["alice"],
        "slack_channel": "#dev",
    }


def test_group_help_says_no_rename(capsys):
    with pytest.raises(SystemExit) as exc:
        main("group", "--help")
    assert exc.value.code == 0
    out = re.sub(r"\s+", " ", capsys.readouterr().out)
    assert "A group cannot be renamed: its name is the audience of every document and record." in out
