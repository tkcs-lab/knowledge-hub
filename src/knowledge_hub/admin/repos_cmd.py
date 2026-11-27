"""``kh repo``: list, add, set, remove the technical repos (T0a) of ``tech_repos`` in kh.yaml."""

from __future__ import annotations

import argparse
import json
from typing import Any

from knowledge_hub.admin import ops, yamledit
from knowledge_hub.admin.apply import Steps
from knowledge_hub.admin.cli import add_common, run
from knowledge_hub.config import ConfigError, KhConfig
from knowledge_hub.setup.steps import Change


def _split(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _given(value: str | None) -> list[str] | None:
    return None if value is None else _split(value)


def _add(args: argparse.Namespace) -> int:
    name = args.name
    dirs, labels = _given(args.dirs), _given(args.labels)
    audience = args.audience

    async def after(steps: Steps, write: bool) -> list[Change]:
        return await steps.forgejo().sync_tech_repo(name, write=write)

    return run(
        args,
        lambda d: ops.repo_add(d, name, dirs, labels, audience),
        after=after,
        target=f"tech repo {name}",
    )


def _set(args: argparse.Namespace) -> int:
    name = args.name
    if args.dirs is None and args.labels is None and args.audience is None:
        print("admin: nothing to set: pass --dirs, --labels or --audience")
        return 2
    dirs, labels = _given(args.dirs), _given(args.labels)
    audience = args.audience
    return run(
        args,
        lambda d: ops.repo_set(d, name, dirs, labels, audience),
        notes=[
            "restart kh serve first (its worker keeps the old config and would undo the change); "
            f"then its first poll applies it, or run kh rebuild --repo {name} after the restart"
        ],
    )


def _remove(args: argparse.Namespace) -> int:
    name = args.name

    async def check(steps: Steps, config: KhConfig) -> str | None:
        documents = await steps.tech().tech_usage(name)
        if documents == 0:
            return None
        return f"tech repo {name}: removing it deletes {documents} indexed documents; pass --yes to continue"

    async def after(steps: Steps, write: bool) -> list[Change]:
        return [await steps.tech().remove_repo(name, write=write)]

    return run(
        args,
        lambda d: ops.repo_remove(d, name),
        check=None if args.yes else check,
        after=after,
        target=f"tech repo {name}",
        notes=[
            f"the Forgejo repo {name} in the tech org is kept; delete it in Forgejo if you want it gone",
            f"restart kh serve now: until then its worker still indexes {name}",
        ],
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
        {
            "name": name,
            "audience": config.tech_audience(name),
            "dirs": config.tech_dirs(name),
            "labels": config.tech_labels(name),
        }
        for name in config.tech_repo_names
    ]
    if args.json:
        print(json.dumps(rows))
    else:
        print("NAME  AUDIENCE  DIRS  LABELS")
        for r in rows:
            dirs, labels = ",".join(r["dirs"]) or "-", ",".join(r["labels"]) or "-"
            print(f"{r['name']}  {r['audience']}  {dirs}  {labels}")
    return 0


def register(sub: Any) -> None:
    repo = sub.add_parser("repo", help="manage the technical repos (T0a) in kh.yaml")
    cmds = repo.add_subparsers(dest="repo_command", required=True)

    p = cmds.add_parser("list", help="list the technical repos")
    add_common(p, mutating=False)
    p.set_defaults(func=_list)

    p = cmds.add_parser("add", help="add a technical repo that already exists in the tech org")
    p.add_argument("name")
    p.add_argument("--dirs", help="comma separated directories to index (default docs)")
    p.add_argument("--labels", help="comma separated labels")
    p.add_argument("--audience", help="group whose members read its documents (default all)")
    add_common(p, mutating=True)
    p.set_defaults(func=_add)

    p = cmds.add_parser("set", help="change the dirs, labels or audience of a technical repo")
    p.add_argument("name")
    p.add_argument("--dirs", help="comma separated directories to index")
    p.add_argument("--labels", help="comma separated labels; an empty value clears them")
    p.add_argument(
        "--audience",
        help="group whose members read its documents; moves the indexed documents on the next sync",
    )
    add_common(p, mutating=True)
    p.set_defaults(func=_set)

    p = cmds.add_parser("remove", help="remove a technical repo and its indexed documents")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="confirm the deletion of its documents")
    add_common(p, mutating=True)
    p.set_defaults(func=_remove)
