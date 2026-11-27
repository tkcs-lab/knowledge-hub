"""Read API of the panel: ``/api/me``, labels, groups, repos, datasets and pipeline."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from knowledge_hub.config import ConfigError, KhConfig
from knowledge_hub.panel import stats
from knowledge_hub.panel.context import PanelContext


def _config_sha256(ctx: PanelContext) -> str | None:
    path = ctx.settings.config_file
    if path is None:
        return None
    try:
        return sha256(Path(path).read_text().encode()).hexdigest()
    except OSError:
        return None


def _error(status: int, code: str, detail: str = "") -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status)


def _load(ctx: PanelContext) -> KhConfig | JSONResponse:
    try:
        return ctx.load_config()
    except ConfigError as exc:
        return _error(503, "config_invalid", str(exc))


def build_read_routes(ctx: PanelContext) -> list[Route]:
    def with_config(
        handler: Callable[[Request, KhConfig], Awaitable[dict[str, Any]]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            config = _load(ctx)
            if isinstance(config, JSONResponse):
                return config
            return JSONResponse(await handler(request, config))

        return endpoint

    async def me(request: Request) -> Response:
        return JSONResponse(
            {"email": getattr(request.state, "email", None), "config_sha256": _config_sha256(ctx)}
        )

    async def labels(request: Request, config: KhConfig) -> dict[str, Any]:
        usage = await stats.label_usage(ctx.pool)
        archived = config.archived_label_set
        return {
            "labels": [
                {
                    "name": name,
                    "description": description,
                    "archived": name in archived,
                    "documents": usage.get(name, {}).get("documents", 0),
                    "records": usage.get(name, {}).get("records", 0),
                    "tech_repos": [r for r in config.tech_repo_names if name in config.tech_labels(r)],
                }
                for name, description in config.labels.items()
            ],
            "config_sha256": _config_sha256(ctx),
        }

    async def groups(request: Request, config: KhConfig) -> dict[str, Any]:
        per_group = await stats.group_stats(ctx.pool)
        return {
            "groups": [
                {
                    "name": name,
                    "repo": group.repo,
                    "archived": group.archived,
                    "slack_channel": group.slack_channel,
                    "members": list(group.members),
                    "approvers": list(group.approvers),
                    "directory": {
                        provider: {"members": list(ref.members), "approvers": list(ref.approvers)}
                        for provider, ref in group.directory.items()
                    },
                    "stats": per_group.get(name) or stats.empty_group_stats(),
                }
                for name, group in config.groups.items()
            ],
            "people": [{"login": login, "email": email} for login, email in config.people.items()],
            "config_sha256": _config_sha256(ctx),
        }

    async def repos(request: Request, config: KhConfig) -> dict[str, Any]:
        state = await stats.repo_state(ctx.pool)
        counts = await stats.document_counts_by_repo(ctx.pool)

        def row(name: str, **extra: Any) -> dict[str, Any]:
            sha, at = state.get(name, (None, None))
            return {
                "name": name,
                **extra,
                "documents": counts.get(name, 0),
                "last_indexed_sha": sha,
                "indexed_at": at,
            }

        rows = [
            row(config.repo_for_group(g), kind="group", group=g, dirs=None, labels=None,
                archived=config.groups[g].archived)
            for g in config.groups
        ]
        rows += [
            row(r, kind="tech", group=config.tech_audience(r), dirs=config.tech_dirs(r),
                labels=config.tech_labels(r), archived=False)
            for r in config.tech_repo_names
        ]
        return {"tech_org": config.tech_org, "repos": rows, "config_sha256": _config_sha256(ctx)}

    async def datasets(request: Request) -> Response:
        return JSONResponse({"datasets": await stats.dataset_catalog(ctx.pool)})

    async def pipeline(request: Request) -> Response:
        return JSONResponse(await stats.pipeline(ctx.pool))

    return [
        Route("/api/me", me, methods=["GET"]),
        Route("/api/labels", with_config(labels), methods=["GET"]),
        Route("/api/groups", with_config(groups), methods=["GET"]),
        Route("/api/repos", with_config(repos), methods=["GET"]),
        Route("/api/datasets", datasets, methods=["GET"]),
        Route("/api/pipeline", pipeline, methods=["GET"]),
    ]
