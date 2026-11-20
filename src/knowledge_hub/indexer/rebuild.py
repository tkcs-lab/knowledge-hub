"""``kh rebuild`` and ``kh check`` (plan section 3.6)."""

from __future__ import annotations

from typing import Any

from knowledge_hub.config import TECH_AUDIENCE
from knowledge_hub.forgejo import ForgejoError
from knowledge_hub.indexer.sync import sync_repo
from knowledge_hub.indexer.sync_plan import SyncPlan

# A record gets its chunks from a queued job; younger records are not drift yet.
RECORD_GRACE = "5 minutes"


def configured_repos(config: Any) -> list[str]:
    return sorted(config.repo_for_group(name) for name in config.active_groups)


def configured_tech_repos(config: Any) -> list[str]:
    return list(config.tech_repo_names)


async def rebuild(deps: Any, repo: str | None = None) -> dict[str, SyncPlan]:
    """Forget the indexed head of ``repo`` (or of every configured repo) and sync it again."""
    repos = configured_repos(deps.config) + configured_tech_repos(deps.config)
    if repo is not None:
        if repo not in repos:
            if deps.config.group_for_repo(repo) is not None:
                raise ValueError(f"repo {repo} is archived")
            raise ValueError(f"repo {repo} is in no group")
        repos = [repo]
    if deps.reader is None or (
        any(deps.config.is_tech_repo(r) for r in repos) and getattr(deps, "tech_reader", None) is None
    ):
        raise ValueError("KH_FORGEJO_URL is required to read the repositories")
    out: dict[str, SyncPlan] = {}
    for name in repos:
        async with deps.indexer_pool.connection() as conn:
            await conn.execute("DELETE FROM kh.repo_state WHERE repo = %s", (name,))
        out[name] = await sync_repo(
            deps.indexer_pool,
            deps.reader,
            deps.config,
            name,
            deps.embedder,
            chunker_version=deps.settings.chunker_version,
            tech_reader=getattr(deps, "tech_reader", None),
        )
    return out


async def check(deps: Any) -> list[str]:
    """Drift lines between Forgejo, the index and the config; empty when everything agrees."""
    tech_repos = configured_tech_repos(deps.config)
    tech_reader = getattr(deps, "tech_reader", None)
    if deps.reader is None or (tech_repos and tech_reader is None):
        raise ValueError("KH_FORGEJO_URL is required to read the repositories")
    lines: list[str] = []
    async with deps.indexer_pool.connection() as conn:
        for repo in configured_repos(deps.config):
            head = await deps.reader.head_sha(repo, "main")
            cur = await conn.execute("SELECT last_indexed_sha FROM kh.repo_state WHERE repo = %s", (repo,))
            row = await cur.fetchone()
            indexed = row[0] if row else None
            if indexed != head:
                lines.append(f"head of {repo} is {head}, indexed {indexed or 'nothing'}")
        for repo in tech_repos:
            try:
                head = await tech_reader.head_sha(repo, await tech_reader.default_branch(repo))
            except ForgejoError as exc:
                lines.append(f"tech repo {repo} not readable: {exc}")
                continue
            cur = await conn.execute("SELECT last_indexed_sha FROM kh.repo_state WHERE repo = %s", (repo,))
            row = await cur.fetchone()
            indexed = row[0] if row else None
            if indexed != head:
                lines.append(f"head of {repo} is {head}, indexed {indexed or 'nothing'}")
        # A document with a blank body legitimately has no chunks.
        cur = await conn.execute(
            "SELECT d.path, d.repo FROM kh.documents d WHERE btrim(d.body) <> ''"
            " AND NOT EXISTS (SELECT 1 FROM kh.chunks c WHERE c.document_id = d.id) ORDER BY d.repo, d.path"
        )
        lines += [f"document {path} in {repo} has no chunks" for path, repo in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT r.id::text FROM kh.records r WHERE r.ingested_at < now() - %s::interval"
            " AND NOT EXISTS (SELECT 1 FROM kh.chunks c WHERE c.record_id = r.id)"
            " ORDER BY r.ingested_at, r.id",
            (RECORD_GRACE,),
        )
        lines += [f"record {rid} has no chunks" for (rid,) in await cur.fetchall()]
        cur = await conn.execute("SELECT repo, path, audience, tier FROM kh.documents ORDER BY repo, path")
        for repo, path, audience, tier in await cur.fetchall():
            is_tech = deps.config.is_tech_repo(repo)
            wanted = TECH_AUDIENCE if is_tech else deps.config.group_for_repo(repo)
            if wanted != audience:
                says = wanted or "no group"
                fix = f"; run kh rebuild --repo {repo}" if wanted is not None else ""
                lines.append(f"document {path} in {repo} has audience {audience}, config says {says}{fix}")
            wanted_tier = "T0a" if is_tech else "T0"
            if tier != wanted_tier:
                lines.append(f"document {path} in {repo} has tier {tier}, config says {wanted_tier}")
    return lines
