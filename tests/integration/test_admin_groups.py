"""``kh group`` against a real database, a real file and the in-memory Forgejo (plan 45, 3.1, 3.6)."""

import psycopg
import pytest
import yaml

from knowledge_hub.admin import apply
from knowledge_hub.admin import cli as admin_cli
from knowledge_hub.config import parse_config
from knowledge_hub.forgejo.admin import ForgejoAdmin
from knowledge_hub.setup.forgejo import ForgejoStep
from knowledge_hub.setup.postgres import PostgresStep
from tests.support.forgejo_admin_fake import FakeForgejoAdmin
from tests.support.seed import add_record

pytestmark = pytest.mark.integration

DB = "kh_fresh"
TEXT = """# keep
people:
  alice: a@example.test
  bob: b@example.test
groups:
  all:
    members: [alice, bob]
    approvers: [alice]
"""


class Fake(FakeForgejoAdmin):
    """The shared fake plus ``GET /users/{login}`` and ``DELETE /teams/{id}``, which the step also calls."""

    def _routes(self):
        import httpx

        def user_get(m, body, params):
            if m[1] not in self.users:
                return httpx.Response(404, json={"message": "no user"})
            return httpx.Response(200, json=self._user(m[1]))

        def team_delete(m, body, params):
            tid = int(m[1])
            if tid not in self.teams:
                return httpx.Response(404, json={"message": "no team"})
            del self.teams[tid], self.team_members[tid], self.team_repos[tid]
            return httpx.Response(204)

        return [
            (("GET", r"/users/([^/]+)"), user_get),
            (("DELETE", r"/teams/(\d+)"), team_delete),
            *super()._routes(),
        ]


class RealSteps:
    def __init__(self, config, settings, fake):
        self._config, self._settings, self._fake = config, settings, fake
        self._admin = ForgejoAdmin("https://forgejo.test", "admin-tok", "kh", transport=fake.transport)

    def postgres(self):
        return PostgresStep(self._config, self._settings, None)

    def forgejo(self):
        return ForgejoStep(
            self._config, self._admin, webhook_secret=None, bot_token="bot-tok", indexer_token="idx-tok"
        )


@pytest.fixture
def fake():
    f = Fake(org="kh", token="admin-tok")
    for login in ("alice", "bob", "carol"):
        f.add_user(login)
    f.add_user("kh-bot", token="bot-tok")
    f.add_user("kh-indexer", token="idx-tok")
    return f


@pytest.fixture
def cfg(tmp_path, monkeypatch, fresh_db, db_settings, fake):
    monkeypatch.delenv("KH_CONFIG_FILE", raising=False)
    path = tmp_path / "kh.yaml"
    path.write_text(TEXT)
    monkeypatch.setattr(apply, "default_steps_factory", lambda config: RealSteps(config, db_settings, fake))
    return path


def main(*argv):
    return admin_cli.main(list(argv))


def db(fresh_db):
    return psycopg.connect(fresh_db.dsn("kh_migrator", DB), autocommit=True)


def groups(fresh_db):
    with db(fresh_db) as conn:
        return dict(conn.execute("SELECT name, repo FROM kh.groups").fetchall())


def team_names(fake):
    return sorted(t["name"] for t in fake.teams.values() if t["name"] != "Owners")


def members(fake, team):
    return sorted(fake.team_members[fake.team_id(team)])


def test_group_add_member_remove_roundtrip(cfg, fresh_db, fake):
    args = ("--members", "alice,bob", "--approvers", "alice", "--config", str(cfg))
    assert main("group", "add", "dev", *args) == 0
    parsed = parse_config(yaml.safe_load(cfg.read_text()))
    assert parsed.groups["dev"].members == ["alice", "bob"]
    assert "# keep" in cfg.read_text()
    assert groups(fresh_db)["dev"] == "kb-dev"
    assert team_names(fake) == ["dev", "dev-approve"]
    assert members(fake, "dev") == ["alice", "bob"]

    assert main("group", "member", "add", "dev", "carol", "--config", str(cfg)) == 2  # not in people
    assert main("person", "add", "carol", "c@example.test", "--config", str(cfg)) == 0
    assert main("group", "member", "add", "dev", "carol", "--config", str(cfg)) == 0
    assert members(fake, "dev") == ["alice", "bob", "carol"]
    assert main("group", "member", "remove", "dev", "carol", "--config", str(cfg)) == 0
    assert members(fake, "dev") == ["alice", "bob"]

    assert main("group", "remove", "dev", "--config", str(cfg)) == 2  # needs --yes
    assert main("group", "remove", "dev", "--yes", "--config", str(cfg)) == 0
    assert "dev:" not in cfg.read_text()
    assert "dev" not in groups(fresh_db)
    assert team_names(fake) == []
    assert "kb-dev" in fake.repos  # the repo is kept


def test_group_remove_with_records_is_refused_and_file_unchanged(cfg, fresh_db, fake, capsys):
    args = ("--members", "alice", "--approvers", "alice", "--config", str(cfg))
    assert main("group", "add", "dev", *args) == 0
    with db(fresh_db) as conn:
        add_record(conn, audience="dev")
    before = cfg.read_text()
    capsys.readouterr()
    assert main("group", "remove", "dev", "--yes", "--config", str(cfg)) == 2
    out = capsys.readouterr().out
    assert "! group dev: still has 1 records" in out
    assert "kh.yaml unchanged" in out
    assert cfg.read_text() == before
    assert "dev" in groups(fresh_db)
    assert team_names(fake) == ["dev", "dev-approve"]
    # without --yes the pre-check reports the records too
    assert main("group", "remove", "dev", "--config", str(cfg)) == 2
    assert "admin: group dev still has 1 records" in capsys.readouterr().out


def test_leaver_removed_from_archived_group_team(cfg, fresh_db, fake):
    args = ("--members", "alice,bob", "--approvers", "alice", "--config", str(cfg))
    assert main("group", "add", "dev", *args) == 0
    assert main("group", "archive", "dev", "--config", str(cfg)) == 0
    assert fake.repos["kb-dev"]["archived"] is True
    assert members(fake, "dev") == ["alice", "bob"]
    assert main("group", "member", "remove", "dev", "bob", "--config", str(cfg)) == 0
    assert members(fake, "dev") == ["alice"]
    assert team_names(fake) == ["dev", "dev-approve"]  # teams kept
    # person remove --from-groups syncs every group the login was in
    assert main("group", "member", "add", "all", "bob", "--config", str(cfg)) == 0
    assert main("person", "remove", "bob", "--from-groups", "--config", str(cfg)) == 0
    assert "bob" not in cfg.read_text().split("people:")[1].split("groups:")[0]
    assert members(fake, "all") == ["alice"]
