"""``kh label``: add, rename, archive, unarchive, remove, list (plan 45, 3.1)."""

from __future__ import annotations

import argparse
import json
from typing import Any

from knowledge_hub.admin import ops, yamledit
from knowledge_hub.admin.apply import Steps
from knowledge_hub.admin.cli import add_common, run
from knowledge_hub.config import ConfigError
from knowledge_hub.setup.steps import Change


def _apply(name: str) -> Any:
    async def after(steps: Steps, write: bool) -> list[Change]:
        labels = steps.labels()
        return await (labels.apply(False) if write else labels.plan())

    return after


def _remove_after(name: str) -> Any:
    async def after(steps: Steps, write: bool) -> list[Change]:
        labels = steps.labels()
        changes = await (labels.apply(False) if write else labels.plan())
        changes = [c for c in changes if c.text != f"unmanaged label {name}"]
        changes.append(await labels.remove_label(name, write=write))
        return changes

    return after


def _add(args: argparse.Namespace) -> int:
    return run(args, lambda d: ops.label_add(d, args.name, args.description), after=_apply(args.name))


def _rename(args: argparse.Namespace) -> int:
    return run(args, lambda d: ops.label_describe(d, args.name, args.text), after=_apply(args.name))


def _archive(args: argparse.Namespace) -> int:
    return run(args, lambda d: ops.label_archive(d, args.name))


def _unarchive(args: argparse.Namespace) -> int:
    return run(args, lambda d: ops.label_unarchive(d, args.name))


def _remove(args: argparse.Namespace) -> int:
    return run(
        args,
        lambda d: ops.label_remove(d, args.name),
        after=_remove_after(args.name),
        target=f"label {args.name}",
    )


def _list(args: argparse.Namespace) -> int:
    if args.config is None:
        print("admin: no config file: pass --config or set KH_CONFIG_FILE")
        return 2
    try:
        config = yamledit.validate(yamledit.load(args.config).text)
    except ConfigError as exc:
        print(str(exc))
        return 2
    rows = [
        {"name": n, "description": d, "archived": n in config.archived_labels}
        for n, d in config.labels.items()
    ]
    if args.json:
        print(json.dumps(rows))
    else:
        print("NAME  STATE  DESCRIPTION")
        for r in rows:
            print(f"{r['name']}  {'archived' if r['archived'] else 'active'}  {r['description']}")
    return 0


def register(sub: Any) -> None:
    label = sub.add_parser("label", help="manage labels in kh.yaml and the database")
    cmds = label.add_subparsers(dest="label_command", required=True)

    p = cmds.add_parser("add", help="add a label")
    p.add_argument("name")
    p.add_argument("-d", "--description", default="")
    add_common(p, mutating=True)
    p.set_defaults(func=_add)

    p = cmds.add_parser("rename", help="change the description of a label (the key never changes)")
    p.add_argument("name")
    p.add_argument("text")
    add_common(p, mutating=True)
    p.set_defaults(func=_rename)

    for name, func, text in (
        ("archive", _archive, "archive a label"),
        ("unarchive", _unarchive, "unarchive a label"),
        ("remove", _remove, "remove a label that is not in use"),
    ):
        p = cmds.add_parser(name, help=text)
        p.add_argument("name")
        add_common(p, mutating=True)
        p.set_defaults(func=func)

    p = cmds.add_parser("list", help="list labels")
    add_common(p, mutating=False)
    p.set_defaults(func=_list)
