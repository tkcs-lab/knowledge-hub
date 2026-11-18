"""``kh setup plan|apply``: build the steps of plan 3.7 and run them."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import ValidationError

from knowledge_hub.config import KhConfig, load_config
from knowledge_hub.settings import Settings, load_settings
from knowledge_hub.setup.steps import Step, run

STEP_NAMES = ("postgres", "labels", "forgejo", "ollama", "s3")


class _NotSet(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _read(path: Path | None, name: str, *, required: bool) -> str | None:
    if path is None:
        if required:
            raise _NotSet(name)
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise _NotSet(name) from exc


def _slack_urls(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    text = _read(path, "KH_SETUP_SLACK_WEBHOOKS_FILE", required=False)
    bad = ValueError("KH_SETUP_SLACK_WEBHOOKS_FILE is not a mapping of group name to https URL")
    try:
        data = yaml.safe_load(text or "")
    except yaml.YAMLError:
        raise bad from None
    if data is None:
        return {}
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.startswith("https://") for k, v in data.items()
    ):
        raise bad from None
    return data


def _builders(
    settings: Settings, config: KhConfig, create_bucket: bool
) -> dict[str, Callable[[], Step]]:
    def postgres() -> Step:
        from knowledge_hub.setup.postgres import PostgresStep

        dsn = _read(settings.setup_pg_admin_dsn_file, "KH_SETUP_PG_ADMIN_DSN_FILE", required=False)
        return PostgresStep(config, settings, dsn)

    def labels() -> Step:
        from knowledge_hub.setup.labels import LabelsStep

        return LabelsStep(config, settings.dsn("migrator"))

    def forgejo() -> Step:
        from knowledge_hub.forgejo.admin import ForgejoAdmin
        from knowledge_hub.setup.forgejo import ForgejoStep

        if not settings.forgejo_url:
            raise _NotSet("KH_FORGEJO_URL")
        token = _read(
            settings.setup_forgejo_admin_token_file, "KH_SETUP_FORGEJO_ADMIN_TOKEN_FILE", required=True
        )
        assert token is not None
        admin = ForgejoAdmin(settings.forgejo_url, token, config.org)
        return ForgejoStep(
            config,
            admin,
            webhook_secret=_read(settings.webhook_hmac_file, "KH_WEBHOOK_HMAC_FILE", required=False),
            bot_token=_read(settings.forgejo_bot_token_file, "KH_FORGEJO_BOT_TOKEN_FILE", required=False),
            indexer_token=_read(
                settings.forgejo_indexer_token_file, "KH_FORGEJO_INDEXER_TOKEN_FILE", required=False
            ),
            slack_urls=_slack_urls(settings.setup_slack_webhooks_file),
        )

    def ollama() -> Step:
        from knowledge_hub.setup.ollama import OllamaStep

        if not settings.ollama_url:
            raise _NotSet("KH_OLLAMA_URL")
        return OllamaStep(settings)

    def s3() -> Step:
        from knowledge_hub.setup.s3 import S3Step

        return S3Step(settings, create_bucket=create_bucket)

    return {"postgres": postgres, "labels": labels, "forgejo": forgejo, "ollama": ollama, "s3": s3}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kh setup", description="Plan or apply the deployment setup.")
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--config", required=True, type=Path, help="the kh.yaml file")
    parser.add_argument("--only", choices=STEP_NAMES, help="run one step only")
    parser.add_argument("--prune", action="store_true", help="remove objects the config no longer lists")
    parser.add_argument("--create-bucket", action="store_true", help="apply: create a missing S3 bucket")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        config = load_config(args.config)
    except (ValidationError, ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    builders = _builders(settings, config, args.create_bucket)
    names = [args.only] if args.only else list(STEP_NAMES)
    try:
        steps = [builders[name]() for name in names]
    except _NotSet as exc:
        print(f"setup: {exc.name} is not set")
        return 2
    except ValueError as exc:
        print(f"setup: {exc}")
        return 2
    except OSError:
        print("setup: a credential file cannot be read")
        return 2
    return asyncio.run(run(steps, args.mode, args.prune))
