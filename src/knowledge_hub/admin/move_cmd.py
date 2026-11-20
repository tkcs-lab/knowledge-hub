"""``kh move``: move a record or a document between groups, as an administrator (plan 69, 3.3)."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from knowledge_hub.admin.cli import add_common
from knowledge_hub.admin.move import move_item
from knowledge_hub.auth.claims import Caller
from knowledge_hub.config import GROUP_RE, ConfigError, load_config
from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.deps import Deps, build_deps, close_deps
from knowledge_hub.mcp.errors import KhError, ToolCall
from knowledge_hub.settings import load_settings


async def do_move(deps: Deps, email: str, id: str, from_group: str, to_group: str) -> dict[str, Any]:
    """Run the move as administrator ``email``, who may move between groups they do not hold."""
    caller = Caller(
        sub=f"admin:{email}",
        principal_type="admin",
        email=email,
        audiences=frozenset({from_group, to_group}),
        token_id=None,
        tools=None,
    )
    async with caller_tx(deps.app_pool, caller) as conn:
        return await move_item(ToolCall(conn, caller, deps, []), id, from_group, to_group)


async def _run(args: argparse.Namespace) -> int:
    email = args.by.lower()
    if args.config is None:
        print("move: --config or KH_CONFIG_FILE is required")
        return 2
    try:
        config = load_config(args.config)
        if email not in config.admins:
            print(f"move: {email} is not an administrator")
            return 1
        for group in (args.from_group, args.to_group):
            if not GROUP_RE.fullmatch(group):
                print("move: group name is not valid")
                return 2
        settings = load_settings()
        deps = await build_deps(settings, config)
        try:
            result = await do_move(deps, email, args.id, args.from_group, args.to_group)
        finally:
            await close_deps(deps)
    except KhError as exc:
        print(f"move: {exc.text}")
        return 1
    except (ValueError, ConfigError) as exc:
        print(f"move: {exc}")
        return 2
    print(json.dumps(result, sort_keys=True))
    return 3 if result.get("note") else 0


def _move(args: argparse.Namespace) -> int:
    return asyncio.run(_run(args))


def register(sub: Any) -> None:
    parser = sub.add_parser("move", help="move a record or a document to another group")
    parser.add_argument("id", help="rec:<uuid> or doc:<uuid>")
    parser.add_argument("--from", dest="from_group", required=True, help="the group the item is in now")
    parser.add_argument("--to", dest="to_group", required=True, help="the group to move it to")
    parser.add_argument("--by", required=True, help="email of the administrator who asks for the move")
    add_common(parser, mutating=False)
    parser.set_defaults(func=_move)
