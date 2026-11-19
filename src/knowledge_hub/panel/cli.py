"""``kh panel``: run the admin panel."""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
import uvicorn
from pydantic import ValidationError

from knowledge_hub.config import load_config
from knowledge_hub.db.pool import make_indexer_pool
from knowledge_hub.oauth.upstream import UpstreamClient, UpstreamConfig
from knowledge_hub.panel.app import build_panel_app
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.session import Signer
from knowledge_hub.panel.settings import PanelSettings
from knowledge_hub.settings import load_settings


async def _serve(port: int) -> int:
    try:
        panel_settings = PanelSettings()
        hub_settings = load_settings()
    except (ValueError, ValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    config_file = panel_settings.config_file
    dev = panel_settings.dev_admin is not None
    signer = None
    upstream = None
    client: httpx.AsyncClient | None = None
    if not dev:
        try:
            signer = Signer(panel_settings.read_session_key())
            assert panel_settings.oidc_client_secret_file is not None
            secret = panel_settings.oidc_client_secret_file.read_text().strip()
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        client = httpx.AsyncClient(timeout=10.0)
        upstream = UpstreamClient(
            UpstreamConfig(
                issuer=panel_settings.oidc_issuer or "",
                client_id=panel_settings.oidc_client_id or "",
                client_secret=secret,
                allowed_domains=panel_settings.allowed_domains_tuple,
                hosted_domains=panel_settings.hosted_domains_tuple,
                redirect_uri=f"{panel_settings.public_url}/auth/callback",
                allow_http=panel_settings.env in ("dev", "test"),
            ),
            client,
        )
    pool = make_indexer_pool(hub_settings)
    try:
        await pool.open()
        ctx = PanelContext(
            settings=panel_settings,
            signer=signer,
            upstream=upstream,
            load_config=lambda: load_config(config_file),
            pool=pool,
            hub_settings=hub_settings,
        )
        app = build_panel_app(ctx)
        server = uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", workers=1)  # noqa: S104
        )
        await server.serve()
        return 0
    finally:
        await pool.close()
        if client is not None:
            await client.aclose()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kh panel", description="the kh admin panel")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the panel web server")
    serve.add_argument("--port", type=int, default=8081)
    args = parser.parse_args(argv)
    return asyncio.run(_serve(args.port))
