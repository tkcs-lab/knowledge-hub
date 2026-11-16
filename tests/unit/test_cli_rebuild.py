"""``kh rebuild --rejudge`` reaches the rebuild function (plan section 3.6)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge_hub import cli


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(config_file=None))
    monkeypatch.setattr(cli, "load_config", lambda path: object())

    def fake(settings, config, repo, rejudge=False):
        seen.append((repo, rejudge))
        return 0

    monkeypatch.setattr(cli, "_rebuild", fake)
    return seen


def test_rejudge_flag_is_passed(calls):
    assert cli.main(["rebuild", "--rejudge", "--repo", "x"]) == 0
    assert calls == [("x", True)]


def test_default_is_no_rejudge(calls):
    assert cli.main(["rebuild", "--repo", "x"]) == 0
    assert calls == [("x", False)]
