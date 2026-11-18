"""``POST /webhooks/forgejo``: verify the signature and queue an index job. The payload is only a trigger."""

from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb
from starlette.requests import Request
from starlette.responses import Response

from knowledge_hub.auth.webhook_hmac import verify_forgejo_signature
from knowledge_hub.mcp.server import Deps
from knowledge_hub.metrics import METRICS


def _empty(status: int, result: str) -> Response:
    METRICS.webhook_event(result)
    return Response(status_code=status)


async def _read_capped(request: Request, cap: int) -> bytes | None:
    """The raw body, or None when it exceeds ``cap`` bytes."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > cap:
            return None
    return bytes(body)


def _secret(deps: Deps) -> bytes | None:
    path = deps.settings.webhook_hmac_file
    if path is None:
        return None
    try:
        return path.read_text().strip().encode() or None
    except OSError:
        return None


def _repo_name(payload: dict[str, Any], org: str) -> str | None:
    """The bare repository name when the push is for ``org``; ``full_name`` first, then owner and name."""
    repo = payload.get("repository")
    if not isinstance(repo, dict):
        return None
    full = repo.get("full_name")
    if not isinstance(full, str) or not full:
        owner = repo.get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        name = repo.get("name")
        if not isinstance(login, str) or not isinstance(name, str):
            return None
        full = f"{login}/{name}"
    if full.count("/") != 1:
        return None
    owner_name, name = full.split("/")
    return name if owner_name == org and name else None


def make_webhook_endpoint(deps: Deps):
    async def forgejo_webhook(request: Request) -> Response:
        body = await _read_capped(request, deps.settings.max_body_bytes)
        if body is None:
            return _empty(413, "too_large")
        secret = _secret(deps)
        if secret is None or not verify_forgejo_signature(
            body, request.headers.get("x-forgejo-signature"), secret
        ):
            return _empty(401, "bad_signature")
        try:
            payload = json.loads(body)
        except ValueError:
            return _empty(400, "bad_request")
        if not isinstance(payload, dict):
            return _empty(400, "bad_request")
        event = request.headers.get("x-forgejo-event") or request.headers.get("x-gitea-event") or ""
        repo = _repo_name(payload, deps.config.org)
        group = deps.config.group_for_repo(repo) if repo is not None else None
        if repo is not None and group is not None:
            if not deps.config.is_active(group):
                return _empty(202, "ignored")
            if event != "push" or payload.get("ref") != "refs/heads/main":
                return _empty(202, "ignored")
        else:
            repo = _repo_name(payload, deps.config.tech_org) if deps.config.tech_org else None
            if repo is None or not deps.config.is_tech_repo(repo):
                return _empty(202, "ignored")
            default_branch = payload["repository"].get("default_branch")
            if (
                event != "push"
                or not isinstance(default_branch, str)
                or not default_branch
                or payload.get("ref") != f"refs/heads/{default_branch}"
            ):
                return _empty(202, "ignored")
        async with deps.indexer_pool.connection() as conn:
            await conn.execute(
                "INSERT INTO kh.index_jobs (kind, repo, payload) VALUES ('git_push', %s, %s)",
                (repo, Jsonb({})),
            )
        return _empty(202, "queued")

    return forgejo_webhook
