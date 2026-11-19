"""Build the real dependencies of the server and the CLI from settings (plan 3.1, 3.2, 3.10)."""

from __future__ import annotations

import logging

import httpx

from knowledge_hub.auth.bearer import BearerResolver
from knowledge_hub.blobs.s3 import S3BlobStore
from knowledge_hub.config import KhConfig
from knowledge_hub.db.pool import make_app_pool, make_indexer_pool
from knowledge_hub.directory.store import MembershipCache
from knowledge_hub.embed.factory import make_embedder
from knowledge_hub.forgejo import ForgejoBot, ForgejoReader
from knowledge_hub.indexer.worker import Worker
from knowledge_hub.mcp.server import Deps
from knowledge_hub.oauth.flow import AuthorizationFlow
from knowledge_hub.oauth.provider import HubAuthProvider
from knowledge_hub.oauth.store import OAuthStore
from knowledge_hub.oauth.upstream import UpstreamClient, UpstreamConfig
from knowledge_hub.ratelimit import RateLimiter
from knowledge_hub.settings import Settings

log = logging.getLogger("knowledge_hub.deps")


def make_blob_store(settings: Settings) -> S3BlobStore | None:
    if not settings.s3_endpoint:
        return None
    if not (settings.s3_bucket and settings.s3_access_key_file and settings.s3_secret_key_file):
        raise ValueError(
            "KH_S3_BUCKET, KH_S3_ACCESS_KEY_FILE and KH_S3_SECRET_KEY_FILE "
            "are required when KH_S3_ENDPOINT is set"
        )
    return S3BlobStore(
        settings.s3_endpoint,
        settings.s3_bucket,
        settings.s3_access_key_file.read_text().strip(),
        settings.s3_secret_key_file.read_text().strip(),
        settings.s3_region,
    )


def make_reader(settings: Settings, config: KhConfig) -> ForgejoReader | None:
    """The read-only Forgejo client of the indexer; None while ``KH_FORGEJO_URL`` is not set."""
    if not settings.forgejo_url:
        return None
    if settings.forgejo_indexer_token_file is None:
        raise ValueError("KH_FORGEJO_INDEXER_TOKEN_FILE is required when KH_FORGEJO_URL is set")
    token = settings.forgejo_indexer_token_file.read_text().strip()
    return ForgejoReader(settings.forgejo_url, token, config.org)


def make_tech_reader(settings: Settings, config: KhConfig) -> ForgejoReader | None:
    """The read-only client for the tech org; None unless ``KH_FORGEJO_URL`` and ``tech_repos`` are set."""
    if not settings.forgejo_url or not config.tech_repos or config.tech_org is None:
        return None
    if settings.forgejo_indexer_token_file is None:
        raise ValueError("KH_FORGEJO_INDEXER_TOKEN_FILE is required when KH_FORGEJO_URL is set")
    token = settings.forgejo_indexer_token_file.read_text().strip()
    return ForgejoReader(settings.forgejo_url, token, config.tech_org)


def make_bot(settings: Settings, config: KhConfig) -> ForgejoBot | None:
    """The write client of the bot account; None unless ``KH_FORGEJO_URL`` and its token file are set."""
    if not settings.forgejo_url or settings.forgejo_bot_token_file is None:
        return None
    token = settings.forgejo_bot_token_file.read_text().strip()
    return ForgejoBot(settings.forgejo_url, token, config.org)


async def build_deps(
    settings: Settings, config: KhConfig, *, upstream_http: httpx.AsyncClient | None = None
) -> Deps:
    """Create every dependency; both pools are open when this returns."""
    embedder = make_embedder(settings)
    blobs = make_blob_store(settings)
    reader = make_reader(settings, config)
    bot = make_bot(settings, config)
    tech_reader = make_tech_reader(settings, config)
    app_pool = make_app_pool(settings)
    indexer_pool = make_indexer_pool(settings)
    try:
        await app_pool.open(wait=True)
        await indexer_pool.open(wait=True)
    except BaseException:
        await app_pool.close()
        await indexer_pool.close()
        if reader is not None:
            await reader.aclose()
        if bot is not None:
            await bot.aclose()
        if tech_reader is not None:
            await tech_reader.aclose()
        raise
    worker = None
    if reader is not None:
        worker = Worker(
            indexer_pool,
            reader,
            embedder,
            config,
            blobs,
            chunker_version=settings.chunker_version,
            tech_reader=tech_reader,
            tech_poll_seconds=settings.tech_poll_minutes * 60,
        )
    membership = MembershipCache(app_pool, config, settings)
    provider = flow = upstream = None
    own_http: httpx.AsyncClient | None = None
    if settings.oauth_enabled:
        http = upstream_http
        if http is None:
            http = own_http = httpx.AsyncClient()
        secret = settings.oidc_client_secret_file.read_text().strip()  # type: ignore[union-attr]
        upstream = UpstreamClient(
            UpstreamConfig(
                issuer=settings.oidc_issuer,  # type: ignore[arg-type]
                client_id=settings.oidc_client_id,  # type: ignore[arg-type]
                client_secret=secret,
                allowed_domains=settings.allowed_domains_tuple,
                hosted_domains=settings.hosted_domains_tuple,
                redirect_uri=f"{settings.public_url}/oauth/callback",
                allow_http=settings.env in ("dev", "test"),
            ),
            http,
        )
        store = OAuthStore(app_pool)
        provider = HubAuthProvider(store, upstream, settings, config, membership)
        flow = AuthorizationFlow(store, upstream, settings, config, membership)
    resource_url = settings.resource_url if provider is not None else None
    bearer = BearerResolver(app_pool, config, provider, membership, resource_url)
    log.info(
        "auth paths: oauth=%s keys=on dev_identity=%s",
        "on" if provider is not None else "off",
        "on" if settings.dev_identity is not None else "off",
    )
    return Deps(
        settings=settings,
        config=config,
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        embedder=embedder,
        blobs=blobs,
        ratelimiter=RateLimiter(settings.rate_limit_per_min),
        reader=reader,
        worker=worker,
        forgejo_bot=bot,
        tech_reader=tech_reader,
        bearer=bearer,
        membership=membership,
        oauth={"provider": provider, "flow": flow, "upstream": upstream, "http": own_http}
        if provider is not None
        else None,
    )


async def close_deps(deps: Deps) -> None:
    if deps.worker is not None:
        deps.worker.stop()
    if deps.oauth and deps.oauth.get("http") is not None:
        await deps.oauth["http"].aclose()
    if deps.reader is not None:
        await deps.reader.aclose()
    if deps.forgejo_bot is not None:
        await deps.forgejo_bot.aclose()
    if deps.tech_reader is not None:
        await deps.tech_reader.aclose()
    if deps.indexer_pool is not None:
        await deps.indexer_pool.close()
    await deps.app_pool.close()
