"""``kh directory sync|plan|people|show`` (plan 58, 3.6)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from pydantic import ValidationError

from knowledge_hub.config import KhConfig, load_config
from knowledge_hub.settings import Settings, load_settings

NOTHING = "directory: provider none; nothing to sync"


def _interval(text: str) -> int:
    value = int(text)
    if not 60 <= value <= 3600:
        raise argparse.ArgumentTypeError("must be between 60 and 3600")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kh directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync")
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--every", type=_interval, metavar="N")
    sync.add_argument("--accept-change", action="store_true")
    sub.add_parser("plan")
    sub.add_parser("people").add_argument("--json", action="store_true")
    sub.add_parser("show")
    return parser


async def _sync(settings: Settings, config: KhConfig, args: argparse.Namespace) -> int:
    from knowledge_hub.config_watch import ConfigWatcher
    from knowledge_hub.db.pool import make_indexer_pool
    from knowledge_hub.directory.provider import ProviderError, build_provider
    from knowledge_hub.directory.sync import ChangeRefused, run_every, sync_once

    name = config.directory.provider
    if name == "none":
        print(NOTHING)
        return 0
    try:
        provider = build_provider(name, settings)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    pool = make_indexer_pool(settings)
    await pool.open()
    try:
        if args.every is not None:
            watcher = None
            if settings.config_file is not None and settings.config_watch_seconds > 0:
                watcher = ConfigWatcher(settings.config_file, config)
            return await run_every(
                config,
                settings,
                provider,
                pool,
                interval=args.every,
                accept_change=args.accept_change,
                watcher=watcher,
            )
        try:
            await sync_once(config, settings, provider, pool, accept_change=args.accept_change)
        except ChangeRefused as exc:
            print(exc, file=sys.stderr)
            return 3
        except ProviderError as exc:
            print(f"directory: {provider.name}: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        await provider.aclose()
        await pool.close()


def _is_stale(config: KhConfig, age: float | None) -> bool:
    return config.directory.provider != "none" and (age is None or age > config.directory.max_stale_seconds)


async def _report(settings: Settings, config: KhConfig, command: str, as_json: bool) -> int:
    from knowledge_hub.directory import store

    membership, age = await store.load_membership(config, settings)
    if command == "show":
        print(f"provider: {config.directory.provider}")
        if age is None:
            print("snapshot: none")
        else:
            users, groups = len(membership.people), len(membership.members)
            print(f"snapshot: {int(age)}s old, {users} users in {groups} groups")
        return 0
    if command == "plan":
        print(f"groups: {len(membership.members)}")
        print(f"people: {len(membership.people)}")
        print(f"skipped: {len(membership.skips)}")
        for skip in membership.skips:
            print(f"skip {skip.group} {skip.role} {skip.who}: {skip.reason}")
        if _is_stale(config, age):
            print(
                "directory: snapshot is missing"
                if age is None
                else f"directory: snapshot is {int(age)}s old",
                file=sys.stderr,
            )
            return 1
        return 0
    rows = [
        {
            "login": login,
            "email": email,
            "source": "static" if login in config.people else "directory",
            "groups": sorted(g for g, m in membership.members.items() if login in m),
            "approver_of": sorted(g for g, m in membership.approvers.items() if login in m),
        }
        for login, email in sorted(membership.people.items())
    ]
    if as_json:
        print(json.dumps(rows))
    else:
        for row in rows:
            print(f"{row['login']}\t{row['email']}\t{row['source']}\t{','.join(row['groups'])}")
    return 0


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        config = load_config(settings.config_file)
    except (ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "sync" and args.every is None and not args.once:
        args.once = True
    if args.command == "sync":
        if config.directory.provider != "none":
            logging.basicConfig(level=logging.INFO, format="%(message)s")
        try:
            return asyncio.run(_sync(settings, config, args))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 0
    return asyncio.run(_report(settings, config, args.command, getattr(args, "json", False)))
