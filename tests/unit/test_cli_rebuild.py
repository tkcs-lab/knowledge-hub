"""Parsing and exit codes of ``kh rebuild``, ``check``, ``daily`` and ``requeue-embeddings``."""

from types import SimpleNamespace

import pytest

from knowledge_hub import cli
from knowledge_hub.indexer import rebuild as rebuild_mod
from knowledge_hub.jobs import daily as daily_mod
from knowledge_hub.jobs import requeue as requeue_mod
from knowledge_hub.jobs import usage as usage_mod

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    for k in [k for k in __import__("os").environ if k.startswith("KH_")]:
        monkeypatch.delenv(k)
    state = SimpleNamespace(closed=0, calls=[])
    deps = SimpleNamespace(indexer_pool="pool", settings=SimpleNamespace(usage_retention_days=180))

    async def build_deps(settings, config):
        return deps

    async def close_deps(d):
        assert d is deps
        state.closed += 1

    monkeypatch.setattr("knowledge_hub.deps.build_deps", build_deps)
    monkeypatch.setattr("knowledge_hub.deps.close_deps", close_deps)
    state.deps = deps
    return state


def test_commands_are_registered():
    assert set(cli.JOB_COMMANDS) == {"rebuild", "check", "daily", "requeue-embeddings"}


def test_rebuild_passes_repo_and_prints(monkeypatch, stubs, capsys):
    async def rebuild(deps, repo=None):
        stubs.calls.append(repo)
        return {"kb-all": SimpleNamespace(head_sha="abc")}

    monkeypatch.setattr(rebuild_mod, "rebuild", rebuild)
    assert cli.main(["rebuild", "--repo", "kb-all"]) == 0
    assert cli.main(["rebuild"]) == 0
    assert stubs.calls == ["kb-all", None]
    assert capsys.readouterr().out == "rebuilt kb-all at abc\n" * 2
    assert stubs.closed == 2


def test_rebuild_unknown_repo_exits_2(monkeypatch, stubs, capsys):
    async def rebuild(deps, repo=None):
        raise ValueError("repo x is in no group")

    monkeypatch.setattr(rebuild_mod, "rebuild", rebuild)
    assert cli.main(["rebuild", "--repo", "x"]) == 2
    assert "in no group" in capsys.readouterr().err
    assert stubs.closed == 1


@pytest.mark.parametrize(("lines", "code"), [([], 0), (["head of kb-all is a, indexed b"], 1)])
def test_check_exit_code_follows_drift(monkeypatch, capsys, lines, code):
    async def check(deps):
        return lines

    monkeypatch.setattr(rebuild_mod, "check", check)
    assert cli.main(["check"]) == code
    assert capsys.readouterr().out == "".join(f"{x}\n" for x in lines)


def test_daily_and_requeue(monkeypatch, capsys):
    async def run_daily(pool):
        assert pool == "pool"
        return 3, 2

    async def requeue(pool):
        assert pool == "pool"
        return 4

    async def purge_usage(pool, days):
        assert pool == "pool"
        assert days == 180
        return 5

    monkeypatch.setattr(daily_mod, "run_daily", run_daily)
    monkeypatch.setattr(usage_mod, "purge_usage", purge_usage)
    monkeypatch.setattr(requeue_mod, "requeue_embeddings", requeue)
    assert cli.main(["daily"]) == 0
    assert cli.main(["requeue-embeddings"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].endswith("; usage events purged: 5")
    assert "3" in out and "2" in out and "queued 4 embed_record jobs" in out


def test_settings_error_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("KH_ENV", "prod")
    monkeypatch.setenv("KH_EMBED_PROVIDER", "fake")
    for cmd in (["rebuild"], ["check"], ["daily"], ["requeue-embeddings"]):
        assert cli.main(cmd) == 2
    assert "KH_EMBED_PROVIDER" in capsys.readouterr().err


def test_config_error_exits_2(monkeypatch, tmp_path, capsys):
    bad = tmp_path / "kh.yaml"
    bad.write_text("nonsense: 1\n")
    monkeypatch.setenv("KH_CONFIG_FILE", str(bad))
    assert cli.main(["check"]) == 2
    assert capsys.readouterr().err


def test_unknown_command_exits_2():
    with pytest.raises(SystemExit) as exc:
        cli.main(["frobnicate"])
    assert exc.value.code == 2
