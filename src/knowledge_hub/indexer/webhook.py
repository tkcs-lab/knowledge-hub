"""``POST /webhooks/forgejo``: verify the signature and queue an index job. The payload is only a trigger."""

from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb
from starlette.requests import Request
from starlette.responses import Response

from knowledge_hub.auth.webhook_hmac import verify_forgejo_signature
from knowledge_hub.mcp.server import Deps


def _empty(status: int) -> Response:
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
    repo = payload.get("repository")
    full = repo.get("full_name") if isinstance(repo, dict) else None
    if not isinstance(full, str) or full.count("/") != 1:
        return None
    owner, name = full.split("/")
    return name if owner == org and name else None


def make_webhook_endpoint(deps: Deps):
    async def forgejo_webhook(request: Request) -> Response:
        body = await _read_capped(request, deps.settings.max_body_bytes)
        if body is None:
            return _empty(413)
        secret = _secret(deps)
        if secret is None or not verify_forgejo_signature(
            body, request.headers.get("x-forgejo-signature"), secret
        ):
            return _empty(401)
        try:
            payload = json.loads(body)
        except ValueError:
            return _empty(400)
        if not isinstance(payload, dict):
            return _empty(400)
        repo = _repo_name(payload, deps.settings.forgejo_org)
        if repo is None or repo not in deps.config.repos:
            return _empty(202)
        event = request.headers.get("x-forgejo-event") or request.headers.get("x-gitea-event") or ""
        kind: str | None = None
        job_payload: dict[str, Any] = {}
        if event == "push" and payload.get("ref") == "refs/heads/main":
            kind = "git_push"
        elif event == "pull_request":
            kind = "pr_event"
            number = payload.get("number")
            if not isinstance(number, int):
                pull = payload.get("pull_request")
                number = pull.get("number") if isinstance(pull, dict) else None
            job_payload = {"repo": repo, "action": payload.get("action"), "number": number}
        if kind is None:
            return _empty(202)
        async with deps.indexer_pool.connection() as conn:
            await conn.execute(
                "INSERT INTO kh.index_jobs (kind, repo, payload) VALUES (%s, %s, %s)",
                (kind, repo, Jsonb(job_payload)),
            )
        return _empty(202)

    return forgejo_webhook
