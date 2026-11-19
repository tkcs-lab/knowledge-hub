"""Health checks of the panel (plan 3.5): nine independent checks, each ``{id, status, detail}``."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx

from knowledge_hub.config_watch import config_digest
from knowledge_hub.db import migrate
from knowledge_hub.directory import store
from knowledge_hub.panel import stats
from knowledge_hub.panel.context import PanelContext

HTTP_TIMEOUT = 3.0
QUEUE_AGE_WARN_S = 600
ALERT_WINDOW_S = 24 * 3600
MESSAGE_CHARS = 200

Check = dict[str, str]
Pending = Callable[[str], list[str]]


def _check(id_: str, status: str, detail: str = "") -> Check:
    return {"id": id_, "status": status, "detail": detail}


def _failure(id_: str, exc: BaseException) -> Check:
    return _check(id_, "fail", f"{type(exc).__name__}: {str(exc)[:MESSAGE_CHARS]}")


class _Memo:
    """Runs an async function once and replays its result or exception."""

    def __init__(self, fn: Callable[[], Awaitable[Any]]) -> None:
        self._fn = fn
        self._done = False
        self._value: Any = None
        self._exc: BaseException | None = None

    async def get(self) -> Any:
        if not self._done:
            try:
                self._value = await self._fn()
            except Exception as exc:
                self._exc = exc
            self._done = True
        if self._exc is not None:
            raise self._exc
        return self._value


async def run_checks(
    ctx: PanelContext,
    *,
    http: httpx.AsyncClient | None = None,
    pending: Pending | None = None,
) -> list[Check]:
    pipeline = _Memo(lambda: stats.pipeline(ctx.pool))
    own_http = http is None
    client = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    async def config() -> Check:
        ctx.load_config()
        return _check("config", "ok", "kh.yaml is valid")

    async def database() -> Check:
        async with ctx.pool.connection() as conn:
            await conn.execute("SELECT 1")
        return _check("database", "ok")

    async def migrations() -> Check:
        if ctx.hub_settings.db_migrator_password_file is None:
            return _check("migrations", "n/a", "no migrator password file")
        try:
            if pending is not None:
                names = pending(ctx.hub_settings.dsn("migrator"))
            else:
                names = await asyncio.to_thread(migrate.pending, ctx.hub_settings.dsn("migrator"))
        except Exception as exc:
            return _check("migrations", "warn", f"{type(exc).__name__}: {str(exc)[:MESSAGE_CHARS]}")
        if names:
            return _check("migrations", "fail", f"{len(names)} pending: {', '.join(names)}")
        return _check("migrations", "ok")

    async def hub() -> Check:
        try:
            resp = await client.get(ctx.settings.hub_url.rstrip("/") + "/healthz")
        except httpx.HTTPError as exc:
            return _check("hub", "fail", f"{type(exc).__name__}: {str(exc)[:MESSAGE_CHARS]}")
        if resp.status_code != 200:
            return _check("hub", "fail", f"healthz answered {resp.status_code}")
        theirs = resp.headers.get("X-KH-Config-Digest")
        if theirs is not None and theirs != config_digest(ctx.load_config()):
            return _check("hub", "warn", "the hub runs an older kh.yaml than the one on disk")
        return _check("hub", "ok")

    async def queue() -> Check:
        data = await pipeline.get()
        dead = data["jobs"]["failed_dead"]
        if dead > 0:
            return _check("queue", "fail", f"{dead} dead jobs")
        stuck = data["stuck_running"]
        age = data["oldest_queued_age_s"]
        if stuck > 0:
            return _check("queue", "warn", f"{stuck} stuck running jobs")
        if age is not None and age > QUEUE_AGE_WARN_S:
            return _check("queue", "warn", f"oldest queued job is {age} s old")
        return _check("queue", "ok")

    async def alerts() -> Check:
        data = await pipeline.get()
        cutoff = ctx.now() - ALERT_WINDOW_S
        recent = [
            a
            for a in data["alerts"]
            if a["created_at"] and datetime.fromisoformat(a["created_at"]).timestamp() >= cutoff
        ]
        if recent:
            return _check("alerts", "warn", f"{len(recent)} alerts in the last 24 h")
        return _check("alerts", "ok")

    async def indexed() -> Check:
        cfg = ctx.load_config()
        state = await stats.repo_state(ctx.pool)
        wanted = [cfg.repo_for_group(g) for g in cfg.active_groups] + cfg.tech_repo_names
        missing = [r for r in wanted if r not in state]
        if missing:
            return _check("indexed", "warn", "not indexed yet: " + ", ".join(missing))
        return _check("indexed", "ok")

    async def embeddings() -> Check:
        n = (await pipeline.get())["records_without_chunks"]
        if n > 0:
            return _check("embeddings", "warn", f"{n} records without chunks")
        return _check("embeddings", "ok")

    async def directory() -> Check:
        cfg = ctx.load_config().directory
        if cfg.provider == "none":
            return _check("directory", "n/a", "no directory provider")
        async with ctx.pool.connection() as conn:
            snap = await store.load_snapshot(conn)
        if snap is None:
            return _check("directory", "fail", "no snapshot")
        age = ctx.now() - snap[1].timestamp()
        if age > cfg.max_stale_seconds:
            return _check("directory", "fail", f"snapshot is {int(age)} s old")
        if age > 2 * cfg.refresh_seconds:
            return _check("directory", "warn", f"snapshot is {int(age)} s old")
        return _check("directory", "ok")

    steps: list[tuple[str, Callable[[], Awaitable[Check]]]] = [
        ("config", config),
        ("database", database),
        ("migrations", migrations),
        ("hub", hub),
        ("queue", queue),
        ("alerts", alerts),
        ("indexed", indexed),
        ("embeddings", embeddings),
        ("directory", directory),
    ]
    results: list[Check] = []
    try:
        for id_, fn in steps:
            try:
                results.append(await fn())
            except Exception as exc:
                results.append(_failure(id_, exc))
    finally:
        if own_http:
            await client.aclose()
    return results
