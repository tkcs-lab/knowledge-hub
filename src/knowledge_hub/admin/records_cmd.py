"""``kh records restore``: undo a feed's removal of T3 records, as an administrator (plan 83, 3.6)."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from typing import Any

from knowledge_hub.admin.cli import add_common
from knowledge_hub.auth.claims import Caller
from knowledge_hub.config import GROUP_RE, ConfigError, load_config
from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.deps import Deps, build_deps, close_deps
from knowledge_hub.mcp.errors import KhError
from knowledge_hub.settings import load_settings


async def do_restore(
    deps: Deps, email: str, audience: str, source: str, prefix: str | None, since: datetime | None
) -> dict[str, Any]:
    """Restore the 'delete' records of ``(audience, source)`` as administrator ``email``."""
    caller = Caller(
        sub=f"admin:{email}",
        principal_type="admin",
        email=email,
        audiences=frozenset({audience}),
        token_id=None,
        tools=None,
    )
    refs = [prefix] if prefix is not None else None
    async with caller_tx(deps.app_pool, caller) as conn:
        cur = await conn.execute(
            "SELECT kh.restore_records(%s, %s, %s::text[], %s, %s)",
            (audience, source, refs, refs is not None, since),
        )
        row = await cur.fetchone()
    return {"restored": int(row[0] or 0)}


async def _run(args: argparse.Namespace) -> int:
    since = None
    if args.since is not None:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError:
            print("records: --since is not a valid time")
            return 2
        if since.tzinfo is None:
            print("records: --since needs a time zone")
            return 2
    if args.config is None:
        print("records: --config or KH_CONFIG_FILE is required")
        return 2
    email = args.by.lower()
    try:
        config = load_config(args.config)
        if email not in config.admins:
            print(f"records: {email} is not an administrator")
            return 1
        if not GROUP_RE.fullmatch(args.audience):
            print("records: group name is not valid")
            return 2
        settings = load_settings()
        deps = await build_deps(settings, config)
        try:
            result = await do_restore(deps, email, args.audience, args.source, args.prefix, since)
        finally:
            await close_deps(deps)
    except KhError as exc:
        print(f"records: {exc.text}")
        return 1
    except (ValueError, ConfigError) as exc:
        print(f"records: {exc}")
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


def _restore(args: argparse.Namespace) -> int:
    return asyncio.run(_run(args))


def register(sub: Any) -> None:
    parser = sub.add_parser("records", help="manage removed T3 records (kh records restore)")
    actions = parser.add_subparsers(dest="records_action", required=True)
    p = actions.add_parser("restore", help="restore records a feed removed, before they are purged")
    p.add_argument("--audience", required=True, help="the group the records are in")
    p.add_argument("--source", required=True, help="the source of the records")
    p.add_argument("--prefix", help="only records whose source_ref starts with this")
    p.add_argument("--since", help="only records removed at or after this ISO 8601 time (with a zone)")
    p.add_argument("--by", required=True, help="email of the administrator who asks for the restore")
    add_common(p, mutating=False)
    p.set_defaults(func=_restore)
