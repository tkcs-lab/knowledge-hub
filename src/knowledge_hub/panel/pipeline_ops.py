"""The panel's in-process pipeline operations (plan 63, 3.7): no subprocess, the pool of the context."""

from __future__ import annotations

from typing import Any

from knowledge_hub.config import ConfigError
from knowledge_hub.jobs.rebuild_queue import queue_rebuild
from knowledge_hub.jobs.requeue import requeue_embeddings
from knowledge_hub.jobs.retry import reclaim_stuck, retry_jobs
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.ops import OpError


async def run_inproc(name: str, args: dict[str, Any], ctx: PanelContext) -> str:
    """Run one ``inproc`` op and return its output line; ``OpError`` for a refusal."""
    if ctx.pool is None:
        raise OpError("no database connection")
    if name == "repo.rebuild":
        repo = args["repo"]
        try:
            cfg = ctx.load_config()
        except ConfigError as exc:
            raise OpError(str(exc)) from exc
        configured = {cfg.repo_for_group(g) for g in cfg.active_groups} | set(cfg.tech_repo_names)
        if repo not in configured:
            raise OpError(f"repo {repo} is not configured")
        if await queue_rebuild(ctx.pool, repo):
            return f"queued rebuild of {repo}"
        return f"rebuild of {repo} is already queued"
    if name == "jobs.retry":
        return f"retried {await retry_jobs(ctx.pool, args.get('ids'))} jobs"
    if name == "jobs.reclaim":
        return f"reclaimed {await reclaim_stuck(ctx.pool)} jobs"
    if name == "embeddings.requeue":
        return f"queued {await requeue_embeddings(ctx.pool)} embed_record jobs"
    raise OpError("unknown operation")
