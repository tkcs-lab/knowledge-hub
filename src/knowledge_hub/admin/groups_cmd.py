"""``kh group``: add, remove, archive, unarchive, member, approver, list (plan 45, 3.1, 3.7, 3.8)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

from knowledge_hub.admin import ops, yamledit
from knowledge_hub.admin.apply import Steps
from knowledge_hub.admin.cli import add_common, run
from knowledge_hub.admin.yamledit import Doc
from knowledge_hub.config import ConfigError, KhConfig
from knowledge_hub.setup.steps import Change

NO_RENAME = "A group cannot be renamed: its name is the audience of every document and record."
TOKEN_NOTE = (
    "a token creator must be a member of the group in kh.yaml (docs/runbook.md, Audiences from the hub)"
)


def _logins(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _old_repo(doc: Doc, name: str) -> str | None:
    """The repo of ``name`` in the file as it is before the edit (None when it cannot be told)."""
    try:
        config = yamledit.validate(doc.text)
        return config.repo_for_group(name) if name in config.groups else None
    except (ConfigError, ValueError):
        return None


def _sync(group: str) -> Any:
    async def after(steps: Steps, write: bool) -> list[Change]:
        return await steps.forgejo().sync_group(group, write=write)

    return after


def _accounts(logins: list[str]) -> Any:
    async def check(steps: Steps, config: KhConfig) -> str | None:
        for login in await steps.forgejo().missing_accounts(logins):
            return f"account {login} does not exist in Forgejo; create it first"
        return None

    return check


# group


def _add(args: argparse.Namespace) -> int:
    name = args.name
    members, approvers = _logins(args.members), _logins(args.approvers)

    async def after(steps: Steps, write: bool) -> list[Change]:
        out = await steps.postgres().sync_groups(write=write)
        out.extend(await steps.forgejo().sync_group(name, write=write))
        return out

    return run(
        args,
        lambda d: ops.group_add(d, name, members, approvers, args.repo, args.slack_channel),
        check=_accounts(list(dict.fromkeys([*members, *approvers]))),
        after=after,
        target=f"group {name}",
        notes=[TOKEN_NOTE],
    )


def _remove(args: argparse.Namespace) -> int:
    name = args.name
    repo: list[str] = []
    notes: list[str] = []

    def edit(doc: Doc) -> bool:
        repo[:] = [_old_repo(doc, name) or f"kb-{name}"]
        notes[:] = [f"the Forgejo repo {repo[0]} is kept; delete it in Forgejo if you want it gone"]
        return ops.group_remove(doc, name)

    async def check(steps: Steps, config: KhConfig) -> str | None:
        documents, records = await steps.postgres().group_usage(name)
        if records:
            return f"group {name} still has {records} records"
        return (
            f"group {name}: removing it deletes {documents} documents from the index and keeps "
            f"the Forgejo repo {repo[0]}; pass --yes to continue"
        )

    async def after(steps: Steps, write: bool) -> list[Change]:
        change = await steps.postgres().remove_group(name, write=write)
        if change.prefix == "!":
            return [change]
        return [change, *await steps.forgejo().remove_group(name, repo=repo[0], write=write)]

    return run(
        args,
        edit,
        check=None if args.yes else check,
        after=after,
        target=f"group {name}",
        notes=notes,
    )


def _archive(args: argparse.Namespace) -> int:
    return run(args, lambda d: ops.group_set_archived(d, args.name, True), after=_sync(args.name))


def _unarchive(args: argparse.Namespace) -> int:
    notes: list[str] = []

    def edit(doc: Doc) -> bool:
        notes[:] = [f"run kh rebuild --repo {_old_repo(doc, args.name) or 'kb-' + args.name}"]
        return ops.group_set_archived(doc, args.name, False)

    return run(args, edit, after=_sync(args.name), notes=notes)


# members and approvers


def _member(add: bool) -> Callable[[argparse.Namespace], int]:
    def command(args: argparse.Namespace) -> int:
        if add:
            return run(
                args,
                lambda d: ops.member_add(d, args.group, args.login),
                check=_accounts([args.login]),
                after=_sync(args.group),
            )
        return run(
            args,
            lambda d: ops.member_remove(d, args.group, args.login, args.with_approver),
            after=_sync(args.group),
        )

    return command


def _approver(add: bool) -> Callable[[argparse.Namespace], int]:
    def command(args: argparse.Namespace) -> int:
        if add:
            return run(
                args,
                lambda d: ops.approver_add(d, args.group, args.login),
                check=_accounts([args.login]),
                after=_sync(args.group),
            )
        return run(args, lambda d: ops.approver_remove(d, args.group, args.login), after=_sync(args.group))

    return command


# list


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
        {
            "name": name,
            "repo": config.repo_for_group(name),
            "archived": g.archived,
            "members": list(g.members),
            "approvers": list(g.approvers),
            "slack_channel": g.slack_channel,
        }
        for name, g in config.groups.items()
    ]
    if args.json:
        print(json.dumps(rows))
    else:
        print("NAME  REPO  STATE  MEMBERS  APPROVERS  SLACK")
        for r in rows:
            print(
                f"{r['name']}  {r['repo']}  {'archived' if r['archived'] else 'active'}  "
                f"{','.join(r['members'])}  {','.join(r['approvers'])}  {r['slack_channel'] or '-'}"
            )
    return 0


def register(sub: Any) -> None:
    group = sub.add_parser(
        "group", help="manage groups in kh.yaml, the database and Forgejo", description=NO_RENAME
    )
    cmds = group.add_subparsers(dest="group_command", required=True)

    p = cmds.add_parser("add", help="add a group")
    p.add_argument("name")
    p.add_argument("--members", required=True, help="comma separated logins")
    p.add_argument("--approvers", required=True, help="comma separated logins")
    p.add_argument("--repo")
    p.add_argument("--slack-channel")
    add_common(p, mutating=True)
    p.set_defaults(func=_add)

    p = cmds.add_parser("remove", help="remove a group without records (the Forgejo repo is kept)")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="confirm the removal")
    add_common(p, mutating=True)
    p.set_defaults(func=_remove)

    for name, func, text in (
        ("archive", _archive, "archive a group"),
        ("unarchive", _unarchive, "unarchive a group"),
    ):
        p = cmds.add_parser(name, help=text)
        p.add_argument("name")
        add_common(p, mutating=True)
        p.set_defaults(func=func)

    for noun, factory in (("member", _member), ("approver", _approver)):
        p = cmds.add_parser(noun, help=f"add or remove a {noun}")
        verbs = p.add_subparsers(dest=f"{noun}_command", required=True)
        for verb in ("add", "remove"):
            v = verbs.add_parser(verb, help=f"{verb} a {noun}")
            v.add_argument("group")
            v.add_argument("login")
            if noun == "member" and verb == "remove":
                v.add_argument("--with-approver", action="store_true", help="also remove them as approver")
            add_common(v, mutating=True)
            v.set_defaults(func=factory(verb == "add"))

    p = cmds.add_parser("list", help="list groups")
    add_common(p, mutating=False)
    p.set_defaults(func=_list)
