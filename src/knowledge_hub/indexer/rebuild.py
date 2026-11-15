"""``kh rebuild`` and ``kh check`` (plan section 3.6)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from psycopg.rows import dict_row

from knowledge_hub.config import Config
from knowledge_hub.indexer.sync import sync_repo
from knowledge_hub.indexer.sync_plan import BRANCH

CONFIG_REPO = "__config__"

# Chunks that differ from their owner row (plan 3.3, the drift query).
DRIFT_SQL = """
SELECT c.id FROM kh.chunks c JOIN kh.documents d ON d.id = c.document_id
 WHERE (c.owner_key, c.tier, c.domain, c.status, c.stale, c.superseded, c.entities,
        c.owner_created_at, c.required_audiences, c.allowed_emails)
       IS DISTINCT FROM ('doc:' || d.id, d.tier, d.domain, d.status, d.stale, d.status = 'superseded',
                         d.entities, d.indexed_at, d.required_audiences, d.allowed_emails)
UNION ALL
SELECT c.id FROM kh.chunks c JOIN kh.raw_records r ON r.id = c.raw_id
 WHERE (c.owner_key, c.tier, c.domain, c.status, c.stale, c.superseded, c.entities,
        c.owner_created_at, c.required_audiences, c.allowed_emails)
       IS DISTINCT FROM ('raw:' || r.id, 'T3'::text, r.domain, 'current'::text, false, false,
                         r.entities, r.ingested_at, r.required_audiences, r.allowed_emails)
UNION ALL
SELECT c.id FROM kh.chunks c JOIN kh.derived_records r ON r.id = c.derived_id
 WHERE (c.owner_key, c.tier, c.domain, c.status, c.stale, c.superseded, c.entities,
        c.owner_created_at, c.required_audiences, c.allowed_emails)
       IS DISTINCT FROM ('derived:' || r.id, 'T2'::text, r.domain, 'current'::text, false, false,
                         r.entities, r.created_at, r.required_audiences, r.allowed_emails)
"""


def config_hash(config: Config) -> str:
    """Hash of the audience map, the repo audiences and the entity visibility."""
    payload = {
        "raw_audience_map": {k: sorted(v) for k, v in sorted(config.raw_audience_map.items())},
        "repos": {name: r.audience for name, r in sorted(config.repos.items())},
        "entities": {k: sorted(v) for k, v in sorted(config.entities.items())},
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class CheckReport:
    repo_drift: list[str] = field(default_factory=list)
    document_drift: list[str] = field(default_factory=list)
    chunk_drift: list[str] = field(default_factory=list)
    config_drift: bool = False

    @property
    def clean(self) -> bool:
        return not (self.repo_drift or self.document_drift or self.chunk_drift or self.config_drift)

    def lines(self) -> list[str]:
        out = [f"repo {r}: head differs from the index" for r in self.repo_drift]
        out += [f"document {d}: content differs from the index" for d in self.document_drift]
        out += [f"chunk {c} differs from its owner" for c in self.chunk_drift]
        if self.config_drift:
            out.append("config hash differs from the index; run kh recompute-visibility")
        return out


async def _sync(deps: Any, name: str) -> Any:
    return await sync_repo(
        deps.indexer_pool,
        deps.forgejo,
        deps.config,
        name,
        deps.embedder,
        chunker_version=deps.settings.chunker_version,
    )


async def rebuild(deps: Any, repo: str | None = None) -> list[str]:
    """Re-walk trust and sync each configured repo's head; documents absent from the tree are deleted."""
    if deps.forgejo is None:
        raise ValueError("KH_FORGEJO_URL is required")
    repos = [repo] if repo is not None else sorted(deps.config.repos)
    if repo is not None and repo not in deps.config.repos:
        raise ValueError(f"repo {repo} is not listed in config")
    for name in repos:
        plan = await _sync(deps, name)
        if plan.refused:
            # Rebuild is the operator's answer to a rewritten main: forget the old head and index anew.
            async with deps.indexer_pool.connection() as conn:
                await conn.execute(
                    "UPDATE kh.repo_state SET last_indexed_sha = NULL WHERE repo = %s", (name,)
                )
            await _sync(deps, name)
    return repos


async def check(deps: Any) -> CheckReport:
    """Compare the index with Forgejo HEAD, the stored chunks and the configuration."""
    if deps.forgejo is None:
        raise ValueError("KH_FORGEJO_URL is required")
    report = CheckReport()
    async with deps.indexer_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT repo, last_indexed_sha, config_hash FROM kh.repo_state")
        state = {r["repo"]: r for r in await cur.fetchall()}
        await cur.execute("SELECT repo, doc_key, path, content_sha FROM kh.documents ORDER BY repo, doc_key")
        docs = await cur.fetchall()
        await cur.execute(DRIFT_SQL)
        report.chunk_drift = sorted(str(r["id"]) for r in await cur.fetchall())
    for name in sorted(deps.config.repos):
        head = await deps.forgejo.head_sha(name, BRANCH)
        if (state.get(name) or {}).get("last_indexed_sha") != head:
            report.repo_drift.append(name)
        for d in (d for d in docs if d["repo"] == name):
            try:
                text = await deps.forgejo.get_raw(name, d["path"], head)
            except Exception:  # missing at head (or unreadable): the index is stale
                report.document_drift.append(f"{name}/{d['path']}")
                continue
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != d["content_sha"]:
                report.document_drift.append(f"{name}/{d['path']}")
    report.config_drift = (state.get(CONFIG_REPO) or {}).get("config_hash") != config_hash(deps.config)
    return report
