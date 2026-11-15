"""The ``kh`` command line."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from pydantic import ValidationError

from knowledge_hub.config import Config, load_config
from knowledge_hub.settings import Settings, load_settings

COMMANDS = ("serve", "migrate", "rebuild", "check", "daily", "recompute-visibility", "requeue-embeddings")


def _not_implemented() -> int:
    print("not implemented yet")
    return 3


async def _serve(settings: Settings, config: Config, port: int) -> None:
    import uvicorn

    from knowledge_hub.app import build_app
    from knowledge_hub.deps import build_deps, close_deps

    deps = await build_deps(settings, config)
    try:
        app = build_app(settings, deps, deps.verifier)
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info"))
        await server.serve()
    finally:
        await close_deps(deps)


async def _with_deps(settings: Settings, config: Config, fn: Any) -> Any:
    from knowledge_hub.deps import build_deps, close_deps

    deps = await build_deps(settings, config)
    try:
        return await fn(deps)
    finally:
        await close_deps(deps)


def _migrate(settings: Settings) -> int:
    from knowledge_hub.db.migrate import MigrationError, migrate

    try:
        done = migrate(settings.dsn("migrator"))
    except MigrationError as exc:
        print(exc, file=sys.stderr)
        return 1
    for name in done:
        print(f"applied {name}")
    return 0


def _rebuild(settings: Settings, config: Config, repo: str | None) -> int:
    from knowledge_hub.indexer.rebuild import rebuild

    done = asyncio.run(_with_deps(settings, config, lambda d: rebuild(d, repo)))
    for name in done:
        print(f"rebuilt {name}")
    return 0


def _check(settings: Settings, config: Config) -> int:
    from knowledge_hub.indexer.rebuild import check

    report = asyncio.run(_with_deps(settings, config, check))
    for line in report.lines():
        print(line)
    return 0 if report.clean else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kh")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name)
        if name == "serve":
            p.add_argument("--port", type=int, default=8080)
        if name == "rebuild":
            p.add_argument("--repo")
    args = parser.parse_args(argv)
    if args.command == "serve":
        try:
            settings = load_settings()
            config = load_config(settings.config_file)
        except (ValidationError, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 2
        try:
            asyncio.run(_serve(settings, config, args.port))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            pass
        return 0
    if args.command not in ("migrate", "rebuild", "check"):
        return _not_implemented()
    try:
        settings = load_settings()
        config = load_config(settings.config_file)
    except (ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "migrate":
        return _migrate(settings)
    try:
        if args.command == "rebuild":
            return _rebuild(settings, config, args.repo)
        return _check(settings, config)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
