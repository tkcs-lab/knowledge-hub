"""``kh label|group|person``: argument parsing and dispatch (plan 45, 3.1, 3.2)."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any

from knowledge_hub.admin import apply
from knowledge_hub.admin.ops import OpError
from knowledge_hub.admin.yamledit import Doc

COMMAND_MODULES = ("labels_cmd", "persons_cmd", "groups_cmd")


def add_common(parser: argparse.ArgumentParser, *, mutating: bool) -> None:
    env = os.environ.get("KH_CONFIG_FILE")
    parser.add_argument("--config", type=Path, default=Path(env) if env else None, help="path of kh.yaml")
    if mutating:
        parser.add_argument("--dry-run", action="store_true", help="print the diff and plan, write nothing")
        parser.add_argument("--no-apply", action="store_true", help="only edit the file")
    else:
        parser.add_argument("--json", action="store_true")


def run(args: argparse.Namespace, edit: Any, **kw: Any) -> int:
    def guarded(doc: Doc) -> bool:
        try:
            return bool(edit(doc))
        except OpError as exc:
            raise _Refused(str(exc)) from exc

    try:
        return asyncio.run(
            apply.execute(args.config, guarded, dry_run=args.dry_run, no_apply=args.no_apply, **kw)
        )
    except _Refused as exc:
        print(f"admin: {exc}")
        return 2


class _Refused(Exception):
    pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kh")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMAND_MODULES:
        importlib.import_module(f"knowledge_hub.admin.{name}").register(sub)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
