"""``kh key create|list|revoke`` (plan 3.7). Connects as ``kh_migrator``; never imported by the server."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import sys
from pathlib import Path

import psycopg
from pydantic import ValidationError

from knowledge_hub.auth.keys import KEY_TOOLS, create_key
from knowledge_hub.config import load_config
from knowledge_hub.settings import load_settings

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$")
HEADER = "id\tname\tgroup\ttools\texpires\tlast_used\tstatus"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kh key")
    sub = parser.add_subparsers(dest="action", required=True)
    c = sub.add_parser("create")
    c.add_argument("--group", required=True)
    c.add_argument("--tools", default="search,ingest_record")
    c.add_argument("--name", required=True)
    c.add_argument("--expires-days", type=int, choices=range(1, 3651), metavar="D")
    c.add_argument("--out", type=Path)
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    sub.add_parser("revoke").add_argument("id")
    return parser


def _fail(text: str) -> int:
    print(text)
    return 1


async def _create(args: argparse.Namespace, settings, config) -> int:
    if not config.is_active(args.group):
        return _fail(f"key: group {args.group} is not an active group")
    tools = [t for t in args.tools.split(",") if t]
    for tool in tools:
        if tool not in KEY_TOOLS:
            return _fail(f"key: unknown tool {tool}")
    if not tools:
        return _fail("key: unknown tool ")
    if not NAME_RE.fullmatch(args.name):
        return _fail(f"key: name {args.name} is not valid")
    async with await psycopg.AsyncConnection.connect(settings.dsn("migrator"), autocommit=True) as conn:
        full = await create_key(
            conn,
            group=args.group,
            tools=sorted(set(tools)),
            name=args.name,
            created_by=f"cli:{getpass.getuser()}",
            expires_days=args.expires_days,
        )
    key_id = full.split(".")[0][4:]
    print(f"created key {key_id} for group {args.group}")
    if args.out is not None:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"key: {full}\n")
        os.chmod(args.out, 0o600)
        print(f"key written to {args.out}")
    else:
        print(f"key: {full}")
        print("shown once: store it as a secret")
    return 0


_LIST_SQL = (
    "SELECT id, name, audience, tools, expires_at, last_used_at,"
    " CASE WHEN revoked_at IS NOT NULL THEN 'revoked'"
    " WHEN expires_at IS NOT NULL AND expires_at <= now() THEN 'expired' ELSE 'active' END"
    " FROM kh.api_keys ORDER BY created_at, id"
)


async def _list(args: argparse.Namespace, settings) -> int:
    async with await psycopg.AsyncConnection.connect(settings.dsn("migrator"), autocommit=True) as conn:
        rows = await (await conn.execute(_LIST_SQL)).fetchall()

    def stamp(value) -> str:
        return value.isoformat() if value is not None else ""

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "id": r[0],
                        "name": r[1],
                        "group": r[2],
                        "tools": list(r[3]),
                        "expires": stamp(r[4]) or None,
                        "last_used": stamp(r[5]) or None,
                        "status": r[6],
                    }
                    for r in rows
                ]
            )
        )
        return 0
    print(HEADER)
    for r in rows:
        print("\t".join([r[0], r[1], r[2], ",".join(r[3]), stamp(r[4]), stamp(r[5]), r[6]]))
    return 0


async def _revoke(args: argparse.Namespace, settings) -> int:
    async with await psycopg.AsyncConnection.connect(settings.dsn("migrator"), autocommit=True) as conn:
        cur = await conn.execute(
            "UPDATE kh.api_keys SET revoked_at = COALESCE(revoked_at, now()) WHERE id = %s RETURNING id",
            (args.id,),
        )
        if await cur.fetchone() is None:
            return _fail(f"key: {args.id} not found")
    print(f"revoked {args.id}")
    return 0


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        config = load_config(settings.config_file)
    except (ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.action == "create":
        return asyncio.run(_create(args, settings, config))
    if args.action == "list":
        return asyncio.run(_list(args, settings))
    return asyncio.run(_revoke(args, settings))
