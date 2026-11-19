"""``POST /api/ops`` and ``GET /api/ops/{id}`` (plan 63, 3.6)."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from knowledge_hub.panel import ops
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.ops import OpError
from knowledge_hub.panel.pipeline_ops import run_inproc
from knowledge_hub.panel.runner import ApiBusy, Runner

BODY_KEYS = {"op", "args", "dry_run", "config_sha256"}


def _error(status: int, code: str, detail: str = "") -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status)


def build_ops_routes(ctx: PanelContext, runner: Runner | None = None) -> list[Route]:
    run = runner if runner is not None else Runner(ctx, inproc=run_inproc)

    async def submit(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "bad_args", "body must be JSON")
        if not isinstance(body, dict):
            return _error(400, "bad_args", "body must be an object")
        extra = sorted(set(body) - BODY_KEYS)
        if extra:
            return _error(400, "bad_args", f"unexpected key: {extra[0][:40]}")
        if "op" not in body or "args" not in body:
            return _error(400, "bad_args", "op and args are required")
        try:
            valid = ops.validate(
                body["op"], body["args"], body.get("dry_run", False), body.get("config_sha256")
            )
        except OpError as exc:
            return _error(400, "bad_args", exc.reason)
        try:
            job_id = await run.submit(valid, request.state.email)
        except ApiBusy:
            return _error(409, "busy", "another operation is running")
        return JSONResponse({"id": job_id, "state": "running"}, status_code=202)

    async def status(request: Request) -> Response:
        job = run.get(request.path_params["id"])
        if job is None:
            return _error(404, "no_such_op")
        return JSONResponse(job)

    return [
        Route("/api/ops", submit, methods=["POST"]),
        Route("/api/ops/{id}", status, methods=["GET"]),
    ]
