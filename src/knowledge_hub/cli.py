"""The ``kh`` command line."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from knowledge_hub.config import KhConfig, load_config
from knowledge_hub.config_watch import EXIT_RESTART, watch
from knowledge_hub.settings import Settings, load_settings

COMMANDS = ("serve", "migrate")
JOB_COMMANDS = ("rebuild", "check", "daily", "requeue-embeddings")


async def _serve(settings: Settings, config: KhConfig, port: int) -> int:
    import uvicorn

    from knowledge_hub.app import build_app
    from knowledge_hub.deps import build_deps, close_deps

    deps = await build_deps(settings, config)
    try:
        app = build_app(settings, deps)
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info"))
        changed: list[bool] = []

        def on_change() -> None:
            server.should_exit = True
            changed.append(True)

        watcher = None
        if settings.config_file is not None and settings.config_watch_seconds > 0:
            watcher = asyncio.create_task(
                watch(settings.config_file, config, settings.config_watch_seconds, on_change)
            )
        try:
            await server.serve()
        finally:
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        return EXIT_RESTART if changed else 0
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


async def _with_deps(settings: Settings, config: KhConfig, run: Any) -> int:
    from knowledge_hub.deps import build_deps, close_deps

    deps = await build_deps(settings, config)
    try:
        return await run(deps)
    finally:
        await close_deps(deps)


async def _rebuild(deps: Any, repo: str | None, queue: bool = False) -> int:
    from knowledge_hub.indexer.rebuild import configured_repos, configured_tech_repos, rebuild

    if queue:
        if repo is None:
            print("kh rebuild --queue needs --repo")
            return 2
        if repo not in configured_repos(deps.config) + configured_tech_repos(deps.config):
            if deps.config.group_for_repo(repo) is not None:
                raise ValueError(f"repo {repo} is archived")
            raise ValueError(f"repo {repo} is in no group")
        from knowledge_hub.jobs.rebuild_queue import queue_rebuild

        if await queue_rebuild(deps.indexer_pool, repo):
            print(f"queued rebuild of {repo}")
        else:
            print(f"rebuild of {repo} is already queued")
        return 0
    for name, plan in (await rebuild(deps, repo)).items():
        print(f"rebuilt {name} at {plan.head_sha}")
    return 0


async def _check(deps: Any) -> int:
    from knowledge_hub.indexer.rebuild import check

    lines = await check(deps)
    for line in lines:
        print(line)
    return 1 if lines else 0


async def _daily(deps: Any) -> int:
    from knowledge_hub.jobs.daily import purge_identity, run_daily
    from knowledge_hub.jobs.usage import purge_usage

    stale = await run_daily(deps.indexer_pool)
    identity = await purge_identity(deps.indexer_pool)
    usage = await purge_usage(deps.indexer_pool, deps.settings.usage_retention_days)
    print(f"stale flags changed: {stale}; identity rows purged: {identity}; usage events purged: {usage}")
    return 0


async def _requeue(deps: Any) -> int:
    from knowledge_hub.jobs.requeue import requeue_embeddings

    print(f"queued {await requeue_embeddings(deps.indexer_pool)} embed_record jobs")
    return 0


def _lint(files: list[str]) -> int:
    """Offline frontmatter check of each file; needs no settings and no config."""
    from knowledge_hub.indexer.frontmatter import Rejected, parse_document

    problems = 0
    for name in files:
        try:
            text = Path(name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"{name}: cannot read file ({type(exc).__name__})")
            problems += 1
            continue
        result = parse_document(text, name)
        if isinstance(result, Rejected):
            print(f"{name}: {result.reason}")
            problems += 1
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "setup":
        from knowledge_hub.setup.cli import main as setup_main

        return setup_main(argv[1:])
    if argv and argv[0] == "panel":
        from knowledge_hub.panel.cli import main as panel_main

        return panel_main(argv[1:])
    if argv and argv[0] in ("label", "group", "person", "repo"):
        from knowledge_hub.admin.cli import main as admin_main

        return admin_main(argv)
    if argv and argv[0] == "directory":
        from knowledge_hub.directory.cli import main as directory_main

        return directory_main(argv[1:])
    if argv and argv[0] == "key":
        from knowledge_hub.auth.keys_cli import main as key_main

        return key_main(argv[1:])
    parser = argparse.ArgumentParser(prog="kh")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name)
        if name == "serve":
            p.add_argument("--port", type=int, default=8080)
    for name in JOB_COMMANDS:
        p = sub.add_parser(name)
        if name == "rebuild":
            p.add_argument("--repo")
            p.add_argument("--queue", action="store_true")
    sub.add_parser("setup", help="plan or apply the deployment setup (kh setup --help)")
    sub.add_parser("panel", help="run the admin panel (kh panel serve)")
    sub.add_parser("lint").add_argument("files", nargs="+", metavar="FILE")
    args = parser.parse_args(argv)
    if args.command == "lint":
        return _lint(args.files)
    try:
        settings = load_settings()
        config = load_config(settings.config_file)
    except (ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "migrate":
        return _migrate(settings)
    jobs = {
        "rebuild": lambda deps: _rebuild(deps, getattr(args, "repo", None), getattr(args, "queue", False)),
        "check": _check,
        "daily": _daily,
        "requeue-embeddings": _requeue,
    }
    if args.command in jobs:
        try:
            return asyncio.run(_with_deps(settings, config, jobs[args.command]))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
    try:
        return asyncio.run(_serve(settings, config, args.port))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
