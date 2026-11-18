"""stage, apply, commit (plan 45, 3.3)."""

import asyncio
import fcntl
import os
from pathlib import Path

import pytest

from knowledge_hub.admin import apply
from knowledge_hub.setup.cli import NotSet
from knowledge_hub.setup.steps import Change

pytestmark = pytest.mark.unit

TEXT = "# keep\nlabels:\n  a: first\npeople:\n  alice: a@x.org\n"


class Fake:
    """A Steps-like object; execute only passes it to check and after."""


def _file(tmp_path: Path) -> Path:
    path = tmp_path / "kh.yaml"
    path.write_text(TEXT)
    return path


def add_label(name="b"):
    def edit(doc):
        doc.data["labels"][name] = "second"
        return True

    return edit


def run(path, edit, **kw):
    kw.setdefault("dry_run", False)
    kw.setdefault("no_apply", False)
    kw.setdefault("steps_factory", lambda config: Fake())
    return asyncio.run(apply.execute(path, edit, **kw))


def test_no_change_writes_nothing(tmp_path, capsys):
    path = _file(tmp_path)
    assert run(path, lambda doc: False) == 0
    assert "nothing to change" in capsys.readouterr().out
    assert path.read_text() == TEXT


def test_no_config_path_is_exit_two(capsys):
    assert run(None, lambda doc: True) == 2
    assert "admin: no config file: pass --config or set KH_CONFIG_FILE" in capsys.readouterr().out


def test_invalid_edit_is_not_written(tmp_path, capsys):
    path = _file(tmp_path)

    def edit(doc):
        doc.data["people"]["bob"] = "not-an-email"
        return True

    assert run(path, edit) == 2
    assert "config:" in capsys.readouterr().out
    assert path.read_text() == TEXT


def test_dry_run_writes_nothing_and_runs_after_with_write_false(tmp_path, capsys):
    path = _file(tmp_path)
    seen = []

    async def after(steps, write):
        seen.append(write)
        return [Change("+", "label b")]

    assert run(path, add_label(), after=after, dry_run=True) == 0
    out = capsys.readouterr().out
    assert seen == [False]
    assert "+  b: second" in out and "+ label b" in out
    assert path.read_text() == TEXT


def test_dry_run_check_refusal_exits_two(tmp_path, capsys):
    path = _file(tmp_path)

    async def check(steps, config):
        return "no way"

    assert run(path, add_label(), check=check, dry_run=True) == 2
    assert "admin: no way" in capsys.readouterr().out


def test_dry_run_not_set_is_a_skip(tmp_path, capsys):
    path = _file(tmp_path)

    async def after(steps, write):
        raise NotSet("KH_SETUP_X")

    assert run(path, add_label(), after=after, dry_run=True) == 0
    assert "plan skipped: KH_SETUP_X is not set" in capsys.readouterr().out
    assert path.read_text() == TEXT


def test_no_apply_commits_without_steps(tmp_path, capsys):
    path = _file(tmp_path)

    def factory(config):
        raise AssertionError("steps must not be built")

    async def after(steps, write):
        raise AssertionError("after must not run")

    assert run(path, add_label(), after=after, no_apply=True, steps_factory=factory) == 0
    out = capsys.readouterr().out
    assert "kh.yaml changed; run kh setup apply" in out
    assert "b: second" in path.read_text() and "# keep" in path.read_text()


def test_check_refusal_writes_nothing(tmp_path, capsys):
    path = _file(tmp_path)

    async def check(steps, config):
        return "account x does not exist"

    assert run(path, add_label(), check=check) == 2
    assert "admin: account x does not exist" in capsys.readouterr().out
    assert path.read_text() == TEXT


def test_target_refusal_keeps_the_old_file(tmp_path, capsys):
    path = _file(tmp_path)

    async def after(steps, write):
        return [Change("!", "label b: in use")]

    assert run(path, add_label(), after=after, target="label b") == 2
    out = capsys.readouterr().out
    assert "! label b: in use" in out and "kh.yaml unchanged" in out
    assert path.read_text() == TEXT


def test_target_prefix_of_other_name_is_not_a_refusal(tmp_path, capsys):
    path = _file(tmp_path)

    async def after(steps, write):
        return [Change("!", "label bb: in use")]

    assert run(path, add_label(), after=after, target="label b") == 2
    out = capsys.readouterr().out
    assert "kh.yaml unchanged" not in out
    assert "kh.yaml changed; 1 lines need your attention" in out
    assert "b: second" in path.read_text()


def test_other_bang_commits_and_exits_two(tmp_path, capsys):
    path = _file(tmp_path)

    async def after(steps, write):
        return [Change("+", "label b"), Change("!", "slack hook r: no URL")]

    assert run(path, add_label(), after=after, target="label b") == 2
    assert "kh.yaml changed; 1 lines need your attention" in capsys.readouterr().out
    assert "b: second" in path.read_text()


def test_after_exception_commits_and_says_incomplete(tmp_path, capsys):
    path = _file(tmp_path)

    async def after(steps, write):
        raise RuntimeError("boom")

    assert run(path, add_label(), after=after) == 2
    out = capsys.readouterr().out
    assert "setup: boom" in out
    assert "kh.yaml changed; apply incomplete: run kh setup apply" in out
    assert "b: second" in path.read_text()


def test_not_set_writes_nothing(tmp_path, capsys):
    path = _file(tmp_path)

    async def after(steps, write):
        raise NotSet("KH_SETUP_PG_ADMIN_DSN_FILE")

    assert run(path, add_label(), after=after) == 2
    assert "admin: KH_SETUP_PG_ADMIN_DSN_FILE is not set" in capsys.readouterr().out
    assert path.read_text() == TEXT


def test_not_set_in_factory_writes_nothing(tmp_path, capsys):
    path = _file(tmp_path)

    def factory(config):
        raise NotSet("KH_X")

    async def after(steps, write):
        return []

    assert run(path, add_label(), after=after, steps_factory=factory) == 2
    assert "admin: KH_X is not set" in capsys.readouterr().out
    assert path.read_text() == TEXT


def test_secrets_in_errors_are_hidden(tmp_path, capsys):
    path = _file(tmp_path)

    async def after(steps, write):
        raise RuntimeError("login failed password=hunter2 for x")

    assert run(path, add_label(), after=after) == 2
    out = capsys.readouterr().out
    assert "hunter2" not in out and "password=***" in out


def test_success_prints_restart_note_and_commits(tmp_path, capsys):
    path = _file(tmp_path)

    async def after(steps, write):
        return [Change("+", "label b")]

    assert run(path, add_label(), after=after) == 0
    out = capsys.readouterr().out
    assert "+ label b" in out and "kh.yaml updated" in out
    assert "restart kh serve: the hub reads kh.yaml at start" in out
    assert "b: second" in path.read_text()


def test_lock_is_held_during_after(tmp_path):
    path = _file(tmp_path)
    result = {}

    async def after(steps, write):
        fd = os.open(tmp_path / ".kh.yaml.lock", os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result["locked"] = False
            except BlockingIOError:
                result["locked"] = True
        finally:
            os.close(fd)
        return []

    assert run(path, add_label(), after=after) == 0
    assert result == {"locked": True}


def test_notes_are_printed_after_success(tmp_path, capsys):
    path = _file(tmp_path)
    assert run(path, add_label(), notes=["note one", "note two"]) == 0
    out = capsys.readouterr().out
    assert out.index("kh.yaml updated") < out.index("note one") < out.index("note two")
    assert out.index("note two") < out.index("restart kh serve")
