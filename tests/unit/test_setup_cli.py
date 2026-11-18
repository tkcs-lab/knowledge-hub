"""``kh setup``: argument parsing, ``--only``, exit codes and missing credentials."""

import pytest

from knowledge_hub import cli
from knowledge_hub.setup import cli as setup_cli
from knowledge_hub.setup.steps import Change

pytestmark = pytest.mark.unit


class FakeStep:
    def __init__(self, name, changes=()):
        self.name = name
        self.changes = list(changes)
        self.calls = []

    async def plan(self):
        self.calls.append("plan")
        return self.changes

    async def apply(self, prune):
        self.calls.append(("apply", prune))
        return self.changes


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith("KH_"):
            monkeypatch.delenv(k)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "kh.yaml"
    path.write_text("{}\n")
    return str(path)


@pytest.fixture
def fakes(monkeypatch):
    steps = {n: FakeStep(n) for n in setup_cli.STEP_NAMES}
    built = []

    def builders(settings, config, create_bucket):
        built.append(create_bucket)
        return {n: (lambda n=n: steps[n]) for n in steps}

    monkeypatch.setattr(setup_cli, "_builders", builders)
    steps["built"] = built
    return steps


def test_parser_requires_mode_and_config(capsys):
    with pytest.raises(SystemExit) as exc:
        setup_cli.main(["plan"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        setup_cli.main(["--config", "x"])
    with pytest.raises(SystemExit):
        setup_cli.main(["plan", "--config", "x", "--only", "nope"])


def test_runs_all_steps_in_order(config, fakes, capsys):
    for name in setup_cli.STEP_NAMES:
        step = fakes[name]
        step.changes = [Change("i", name)]
    assert setup_cli.main(["plan", "--config", config]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"i {n}" for n in setup_cli.STEP_NAMES]


def test_only_runs_one_step(config, fakes):
    assert setup_cli.main(["apply", "--config", config, "--only", "labels", "--prune"]) == 0
    assert fakes["labels"].calls == [("apply", True)]
    assert all(fakes[n].calls == [] for n in ("postgres", "forgejo", "ollama", "s3"))


def test_create_bucket_is_passed(config, fakes):
    setup_cli.main(["apply", "--config", config, "--create-bucket"])
    assert fakes["built"] == [True]


def test_exit_codes(config, fakes):
    fakes["labels"].changes = [Change("+", "label x")]
    assert setup_cli.main(["plan", "--config", config]) == 1
    assert setup_cli.main(["apply", "--config", config]) == 0
    fakes["labels"].changes = [Change("!", "label x: in use")]
    assert setup_cli.main(["apply", "--config", config]) == 2


def test_unreadable_config_is_exit_2(tmp_path, capsys):
    assert setup_cli.main(["plan", "--config", str(tmp_path / "missing.yaml")]) == 2


def test_missing_admin_token_setting(config, monkeypatch, capsys):
    monkeypatch.setenv("KH_FORGEJO_URL", "http://forgejo")
    assert setup_cli.main(["plan", "--config", config, "--only", "forgejo"]) == 2
    assert capsys.readouterr().out == "setup: KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE is not set\n"


def test_missing_forgejo_url(config, capsys):
    assert setup_cli.main(["plan", "--config", config, "--only", "forgejo"]) == 2
    assert capsys.readouterr().out == "setup: KH_FORGEJO_URL is not set\n"


def test_unreadable_token_file_does_not_print_path_content(config, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KH_FORGEJO_URL", "http://forgejo")
    monkeypatch.setenv("KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE", str(tmp_path / "gone"))
    assert setup_cli.main(["plan", "--config", config, "--only", "forgejo"]) == 2
    assert "KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE" in capsys.readouterr().out


def test_missing_ollama_url(config, capsys):
    assert setup_cli.main(["plan", "--config", config, "--only", "ollama"]) == 2
    assert capsys.readouterr().out == "setup: KH_OLLAMA_URL is not set\n"


def test_kh_delegates_setup(monkeypatch):
    seen = []
    monkeypatch.setattr(setup_cli, "main", lambda argv: seen.append(argv) or 7)
    assert cli.main(["setup", "plan", "--config", "x"]) == 7
    assert seen == [["plan", "--config", "x"]]


def test_setup_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        cli.main(["setup", "--help"])
    assert exc.value.code == 0


def _forgejo_env(monkeypatch, tmp_path):
    token = tmp_path / "admin-token"
    token.write_text("tok\n")
    monkeypatch.setenv("KH_FORGEJO_URL", "http://forgejo")
    monkeypatch.setenv("KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE", str(token))


def _build_forgejo(monkeypatch, tmp_path, yaml_text):
    from knowledge_hub.config import parse_config
    from knowledge_hub.settings import load_settings

    _forgejo_env(monkeypatch, tmp_path)
    import yaml

    config = parse_config(yaml.safe_load(yaml_text))
    return setup_cli._builders(load_settings(), config, False)["forgejo"]()


def test_forgejo_step_gets_a_tech_admin_when_tech_repos_are_set(monkeypatch, tmp_path):
    base = (
        "people: {alice: a@example.com}\n"
        "groups: {all: {members: [alice], approvers: [alice]}}\n"
    )
    plain = _build_forgejo(monkeypatch, tmp_path, base)
    assert plain._tech_admin is None
    step = _build_forgejo(
        monkeypatch, tmp_path, base + "tech_org: kh-tech\ntech_repos: {docs-a: {}}\n"
    )
    assert step._tech_admin is not None
    assert step._tech_admin.org == "kh-tech"
    assert step._admin.org == "kh"


def test_step_names_include_tech_after_labels():
    names = setup_cli.STEP_NAMES
    assert names[names.index("labels") + 1] == "tech"
    assert names.index("tech") < names.index("forgejo")


def test_slack_urls_file_parsing(tmp_path):
    msg = "KH_SETUP_SLACK_WEBHOOKS_FILE is not a mapping of group name to https URL"
    assert setup_cli._slack_urls(None) is None

    def read(text):
        path = tmp_path / "slack.yaml"
        path.write_text(text)
        return setup_cli._slack_urls(path)

    assert read("all: https://hooks.slack.com/services/T0/B0/X\n") == {
        "all": "https://hooks.slack.com/services/T0/B0/X"
    }
    assert read("") == {}
    for text in ("- https://hooks.slack.com/a\n", "all: http://hooks.slack.com/a\n", "all: 12\n",
                 "all: [unclosed\n"):
        with pytest.raises(ValueError) as exc:
            read(text)
        assert str(exc.value) == msg
        assert "unclosed" not in str(exc.value) and "hooks.slack.com" not in str(exc.value)


def test_slack_urls_unreadable_file(config, tmp_path, monkeypatch, capsys):
    token = tmp_path / "token"
    token.write_text("tok\n")
    monkeypatch.setenv("KH_FORGEJO_URL", "http://forgejo")
    monkeypatch.setenv("KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE", str(token))
    monkeypatch.setenv("KH_SETUP_SLACK_WEBHOOKS_FILE", str(tmp_path / "gone"))
    assert setup_cli.main(["plan", "--config", config, "--only", "forgejo"]) == 2
    assert capsys.readouterr().out == "setup: KH_SETUP_SLACK_WEBHOOKS_FILE is not set\n"
