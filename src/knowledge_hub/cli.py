"""The ``kh`` command line."""

from __future__ import annotations

import argparse
import asyncio
import sys

from pydantic import ValidationError

from knowledge_hub.config import KhConfig, load_config
from knowledge_hub.settings import Settings, load_settings

COMMANDS = ("serve", "migrate")


async def _serve(settings: Settings, config: KhConfig, port: int) -> None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kh")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name)
        if name == "serve":
            p.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        config = load_config(settings.config_file)
    except (ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "migrate":
        return _migrate(settings)
    try:
        asyncio.run(_serve(settings, config, args.port))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
