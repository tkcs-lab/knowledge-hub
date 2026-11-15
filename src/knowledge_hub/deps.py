"""Build the real dependencies of the server and the CLI from settings (plan 3.1, 3.2, 3.10)."""

from __future__ import annotations

from knowledge_hub.auth.pf_claim_verify import ClaimVerifier
from knowledge_hub.blobs.s3 import S3BlobStore
from knowledge_hub.config import Config
from knowledge_hub.db.pool import make_app_pool, make_indexer_pool
from knowledge_hub.embed.factory import make_embedder
from knowledge_hub.forgejo.client import ForgejoClient
from knowledge_hub.indexer.worker import Worker
from knowledge_hub.mcp.server import Deps
from knowledge_hub.settings import Settings


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


def make_forgejo(settings: Settings) -> ForgejoClient | None:
    if not settings.forgejo_url:
        return None
    if settings.forgejo_indexer_token_file is None:
        raise ValueError("KH_FORGEJO_INDEXER_TOKEN_FILE is required when KH_FORGEJO_URL is set")
    token = settings.forgejo_indexer_token_file.read_text().strip()
    return ForgejoClient(settings.forgejo_url, token, settings.forgejo_org)


def make_verifier(settings: Settings) -> ClaimVerifier | None:
    """The production verifier. Without PrivacyFence settings only the dev identity may stand in."""
    if settings.pf_jwks_url and settings.pf_issuer and settings.pf_audience:
        # Replay protection lives in the middleware (``PgReplayCache``), so the verifier has none.
        return ClaimVerifier(
            issuer=settings.pf_issuer,
            audience=settings.pf_audience,
            jwks_url=settings.pf_jwks_url,
            replay_cache=None,
        )
    if settings.dev_identity is not None:
        return None
    raise ValueError(
        "KH_PF_JWKS_URL, KH_PF_ISSUER and KH_PF_AUDIENCE are required unless KH_DEV_IDENTITY is set"
    )


async def build_deps(settings: Settings, config: Config) -> Deps:
    """Create every dependency; both pools are open when this returns."""
    verifier = make_verifier(settings)
    embedder = make_embedder(settings)
    blobs = make_blob_store(settings)
    forgejo = make_forgejo(settings)
    app_pool = make_app_pool(settings)
    indexer_pool = make_indexer_pool(settings)
    try:
        await app_pool.open(wait=True)
        await indexer_pool.open(wait=True)
    except BaseException:
        await app_pool.close()
        await indexer_pool.close()
        if forgejo is not None:
            await forgejo.aclose()
        raise
    worker = None
    if forgejo is not None:
        worker = Worker(
            indexer_pool, forgejo, embedder, config, blobs, chunker_version=settings.chunker_version
        )
    return Deps(
        settings=settings,
        config=config,
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        embedder=embedder,
        blobs=blobs,
        forgejo=forgejo,
        verifier=verifier,
        worker=worker,
    )


async def close_deps(deps: Deps) -> None:
    if deps.worker is not None:
        deps.worker.stop()
    if deps.forgejo is not None:
        await deps.forgejo.aclose()
    if deps.indexer_pool is not None:
        await deps.indexer_pool.close()
    await deps.app_pool.close()
