"""``kh person``: add, remove, list (plan 45, 3.1)."""

from __future__ import annotations

import argparse
import json
from typing import Any

from knowledge_hub.admin import ops, yamledit
from knowledge_hub.admin.apply import Steps
from knowledge_hub.admin.cli import add_common, run
from knowledge_hub.admin.yamledit import Doc
from knowledge_hub.config import ConfigError
from knowledge_hub.setup.steps import Change


def _add(args: argparse.Namespace) -> int:
    return run(args, lambda d: ops.person_add(d, args.login, args.email))


def _remove(args: argparse.Namespace) -> int:
    left: list[str] = []
    want = args.login.casefold()

    def edit(doc: Doc) -> bool:
        left.clear()
        for name, g in (doc.data.get("groups") or {}).items():
            g = g or {}
            logins = [str(x).casefold() for x in (g.get("members") or []) + (g.get("approvers") or [])]
            if want in logins:
                left.append(str(name))
        return ops.person_remove(doc, args.login, args.from_groups)

    async def after(steps: Steps, write: bool) -> list[Change]:
        out: list[Change] = []
        for group in left:
            out.extend(await steps.forgejo().sync_group(group, write=write))
        return out

    return run(args, edit, after=after if args.from_groups else None)


def _list(args: argparse.Namespace) -> int:
    if args.config is None:
        print("admin: no config file: pass --config or set KH_CONFIG_FILE")
        return 2
    try:
        config = yamledit.validate(yamledit.load(args.config).text)
    except ConfigError as exc:
        print(str(exc))
        return 2
    rows = []
    for login, email in config.people.items():
        groups = [
            g
            for g, c in config.groups.items()
            if login.casefold() in {m.casefold() for m in [*c.members, *c.approvers]}
        ]
        rows.append({"login": login, "email": email, "groups": groups})
    if args.json:
        print(json.dumps(rows))
    else:
        print("LOGIN  EMAIL  GROUPS")
        for r in rows:
            print(f"{r['login']}  {r['email']}  {','.join(r['groups'])}")
    return 0


def register(sub: Any) -> None:
    person = sub.add_parser("person", help="manage people in kh.yaml")
    cmds = person.add_subparsers(dest="person_command", required=True)

    p = cmds.add_parser("add", help="add a person")
    p.add_argument("login")
    p.add_argument("email")
    add_common(p, mutating=True)
    p.set_defaults(func=_add)

    p = cmds.add_parser("remove", help="remove a person")
    p.add_argument("login")
    p.add_argument("--from-groups", action="store_true", help="also remove them from every group")
    add_common(p, mutating=True)
    p.set_defaults(func=_remove)

    p = cmds.add_parser("list", help="list people")
    add_common(p, mutating=False)
    p.set_defaults(func=_list)
