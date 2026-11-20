"""kh move: the run function's exit codes (no database)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from knowledge_hub.admin import move_cmd
from knowledge_hub.config import KhConfig
from knowledge_hub.mcp.errors import KhError

pytestmark = pytest.mark.unit

ID = "rec:00000000-0000-4000-8000-000000000000"


def args(by="Admin@Example.Test", from_group="all", to_group="mgmt", config=Path("kh.yaml")):
    return argparse.Namespace(
        id=ID, from_group=from_group, to_group=to_group, by=by, config=config, json=False
    )


@pytest.fixture
def calls(monkeypatch):
    seen: list = []

    async def build(settings, config):
        return "deps"

    async def close(deps):
        seen.append(("close", deps))

    async def do(deps, email, id, from_group, to_group):
        seen.append(("move", email, id, from_group, to_group))
        return {"id": id, "moved": True, "note": ""}

    monkeypatch.setattr(move_cmd, "load_config", lambda path: KhConfig(admins=["admin@example.test"]))
    monkeypatch.setattr(move_cmd, "load_settings", lambda: object())
    monkeypatch.setattr(move_cmd, "build_deps", build)
    monkeypatch.setattr(move_cmd, "close_deps", close)
    monkeypatch.setattr(move_cmd, "do_move", do)
    return seen


def test_not_an_administrator(calls, capsys):
    assert move_cmd._move(args(by="eve@example.test")) == 1
    assert capsys.readouterr().out.strip() == "move: eve@example.test is not an administrator"
    assert calls == []


def test_bad_group_name(calls, capsys):
    assert move_cmd._move(args(from_group="A_b")) == 2
    assert capsys.readouterr().out.strip() == "move: group name is not valid"
    assert calls == []


def test_config_is_required(calls, capsys):
    assert move_cmd._move(args(config=None)) == 2
    assert "--config or KH_CONFIG_FILE is required" in capsys.readouterr().out


def test_kh_error_is_exit_1(calls, monkeypatch, capsys):
    async def boom(*a):
        raise KhError("not_found", "no such item")

    monkeypatch.setattr(move_cmd, "do_move", boom)
    assert move_cmd._move(args()) == 1
    assert capsys.readouterr().out.strip() == "move: no such item"
    assert calls == [("close", "deps")]


def test_note_is_exit_3(calls, monkeypatch, capsys):
    async def partial(*a):
        return {"note": "partial"}

    monkeypatch.setattr(move_cmd, "do_move", partial)
    assert move_cmd._move(args()) == 3
    assert json.loads(capsys.readouterr().out) == {"note": "partial"}


def test_normal_result_and_by_is_lower_cased(calls, capsys):
    assert move_cmd._move(args()) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out) == {"id": ID, "moved": True, "note": ""}
    assert calls[0] == ("move", "admin@example.test", ID, "all", "mgmt")
