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


def _recompute(settings: Settings, config: Config) -> int:
    from knowledge_hub.jobs.recompute import recompute_visibility

    digest = asyncio.run(_with_deps(settings, config, lambda d: recompute_visibility(d.indexer_pool, config)))
    print(f"recomputed visibility (config hash {digest[:12]})")
    return 0


def _requeue(settings: Settings, config: Config) -> int:
    from knowledge_hub.jobs.requeue import requeue_embeddings

    count = asyncio.run(_with_deps(settings, config, lambda d: requeue_embeddings(d.indexer_pool)))
    print(f"queued {count} embed jobs")
    return 0


def _daily(settings: Settings, config: Config) -> int:
    from datetime import date

    from knowledge_hub.grist.client import GristClient, GristError
    from knowledge_hub.jobs.daily import run_daily

    if not settings.grist_url or not settings.grist_doc_id or settings.grist_key_file is None:
        print("daily: KH_GRIST_URL, KH_GRIST_DOC_ID and KH_GRIST_KEY_FILE must be set", file=sys.stderr)
        return 2
    key = settings.grist_key_file.read_text().strip()
    grist = GristClient(settings.grist_url, settings.grist_doc_id, key)

    async def go(deps: Any) -> dict[str, int]:
        try:
            return await run_daily(deps.indexer_pool, grist, config, date.today())
        finally:
            await grist.aclose()

    try:
        result = asyncio.run(_with_deps(settings, config, go))
    except GristError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("daily: " + ", ".join(f"{k}={v}" for k, v in result.items()))
    return 0


def _governance(args: argparse.Namespace) -> int:
    from pathlib import Path

    from knowledge_hub.governance.render import render_governance

    out = Path(args.out)
    try:
        written = render_governance(
            out,
            repo=args.repo,
            audience=args.audience,
            org=args.org,
            portfolio_team=args.portfolio_team,
            product_team=args.product_team,
            technical_team=args.technical_team,
            owner_team=args.owner_team,
            admin_team=args.admin_team,
            review_trigger=args.review_trigger,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    for path in written:
        print(f"wrote {path.relative_to(out)}")
    return 0


def _make_forgejo(url: str, token: str, org: str) -> Any:
    from knowledge_hub.forgejo import ForgejoClient

    return ForgejoClient(url, token, org)


def _make_grist(url: str, doc_id: str, key: str) -> Any:
    from knowledge_hub.grist.client import GristClient

    return GristClient(url, doc_id, key)


def _validate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from knowledge_hub.forgejo import ForgejoError
    from knowledge_hub.governance.ci import CiError, run_validate
    from knowledge_hub.grist.client import GristError

    def fail(message: str) -> int:
        print(f"validate: {message}", file=sys.stderr)
        return 2

    try:
        settings = load_settings()
        config = load_config(args.config or settings.config_file)
        url = args.forgejo_url or settings.forgejo_url
        org = args.forgejo_org or settings.forgejo_org
        grist_url = args.grist_url or settings.grist_url
        grist_key_file = args.grist_key_file or settings.grist_key_file
        grist_doc = args.grist_doc_id or settings.grist_doc_id
        for name, value in (
            ("--forgejo-url", url),
            ("--token-file", args.token_file),
            ("--grist-url", grist_url),
            ("--grist-key-file", grist_key_file),
            ("--grist-doc-id", grist_doc),
        ):
            if not value:
                return fail(f"{name} is required")
        token = Path(args.token_file).read_text().strip()
        status_token = Path(args.status_token_file).read_text().strip() if args.status_token_file else None
        grist_key = Path(grist_key_file).read_text().strip()
    except (OSError, ValueError) as exc:
        return fail(str(exc))

    async def go() -> int:
        read = _make_forgejo(url, token, org)
        status = _make_forgejo(url, status_token, org) if status_token else read
        grist = _make_grist(grist_url, grist_doc, grist_key)
        try:
            result = await run_validate(
                read, status, grist, config, args.repo, args.base, args.head, post=not args.no_status
            )
        finally:
            await grist.aclose()
            if status is not read:
                await status.aclose()
            await read.aclose()
        for line in result.lines:
            print(line)
        return 0 if result.ok else 1

    try:
        return asyncio.run(go())
    except (ForgejoError, GristError, CiError, OSError, ValueError) as exc:
        return fail(str(exc))


def _domain_owners(args: argparse.Namespace) -> int:
    from pathlib import Path

    from knowledge_hub.forgejo import ForgejoError
    from knowledge_hub.governance.ci import CiError, run_domain_owners

    def fail(message: str) -> int:
        print(f"domain-owners: {message}", file=sys.stderr)
        return 2

    try:
        settings = load_settings()
        config = load_config(args.config or settings.config_file)
        url = args.forgejo_url or settings.forgejo_url
        org = args.forgejo_org or settings.forgejo_org
        for name, value in (("--forgejo-url", url), ("--token-file", args.token_file)):
            if not value:
                return fail(f"{name} is required")
        token = Path(args.token_file).read_text().strip()
        status_token = Path(args.status_token_file).read_text().strip() if args.status_token_file else None
    except (OSError, ValueError) as exc:
        return fail(str(exc))

    async def go() -> int:
        read = _make_forgejo(url, token, org)
        status = _make_forgejo(url, status_token, org) if status_token else read
        try:
            result = await run_domain_owners(
                read, status, config, args.repo, args.pr, post=not args.no_status
            )
        finally:
            if status is not read:
                await status.aclose()
            await read.aclose()
        for line in result.lines:
            print(line)
        return 0 if result.ok else 1

    try:
        return asyncio.run(go())
    except (ForgejoError, CiError, OSError, ValueError, KeyError) as exc:
        return fail(str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kh")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name)
        if name == "serve":
            p.add_argument("--port", type=int, default=8080)
        if name == "rebuild":
            p.add_argument("--repo")
    gov = sub.add_parser("governance").add_subparsers(dest="governance_command", required=True)
    render = gov.add_parser("render")
    for flag in ("--repo", "--audience", "--out"):
        render.add_argument(flag, required=True)
    render.add_argument("--org", default="kh")
    render.add_argument("--portfolio-team", default="owners-portfolio")
    render.add_argument("--product-team", default="owners-product")
    render.add_argument("--technical-team", default="owners-technical")
    render.add_argument("--owner-team")
    render.add_argument("--admin-team", default="kh-admins")
    render.add_argument("--review-trigger", action="store_true")
    val = sub.add_parser("validate")
    for flag in ("--repo", "--base", "--head"):
        val.add_argument(flag, required=True)
    for flag in (
        "--forgejo-url", "--forgejo-org", "--token-file", "--status-token-file",
        "--grist-url", "--grist-key-file", "--grist-doc-id", "--config",
    ):
        val.add_argument(flag)
    val.add_argument("--no-status", action="store_true")
    own = sub.add_parser("domain-owners")
    own.add_argument("--repo", required=True)
    own.add_argument("--pr", required=True, type=int)
    for flag in ("--forgejo-url", "--forgejo-org", "--token-file", "--status-token-file", "--config"):
        own.add_argument(flag)
    own.add_argument("--no-status", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _validate(args)
    if args.command == "domain-owners":
        return _domain_owners(args)
    if args.command == "governance":
        return _governance(args)
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
    if args.command not in (
        "migrate", "rebuild", "check", "daily", "recompute-visibility", "requeue-embeddings"
    ):
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
        if args.command == "daily":
            return _daily(settings, config)
        if args.command == "recompute-visibility":
            return _recompute(settings, config)
        if args.command == "requeue-embeddings":
            return _requeue(settings, config)
        return _check(settings, config)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
