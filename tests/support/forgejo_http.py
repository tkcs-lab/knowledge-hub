"""HTTP-level fakes for ``kh validate``: a Forgejo API and a Grist API on ``httpx.MockTransport``."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

import httpx

_REPO = r"/api/v1/repos/(?P<org>[^/]+)/(?P<repo>[^/]+)"


class FakeForgejoHttp:
    """``repos`` is ``{repo: {"sha", "tree": {path: text}, "compare": [...]}}``.

    Every request lands in ``requests``; every ``POST .../statuses/<sha>`` body in ``statuses``
    (its target in ``status_targets``). ``fail`` maps a path substring to an HTTP status to return.
    """

    def __init__(self, repos: dict[str, dict[str, Any]], fail: dict[str, int] | None = None) -> None:
        self.repos = repos
        self.fail = fail or {}
        self.requests: list[httpx.Request] = []
        self.statuses: list[dict[str, Any]] = []
        self.status_targets: list[tuple[str, str]] = []
        self.transport = httpx.MockTransport(self._handle)

    def paths(self, kind: str) -> list[str]:
        return [r.url.path for r in self.requests if kind in r.url.path]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode().split("?")[0]
        for needle, code in self.fail.items():
            if needle in path:
                return httpx.Response(code, json={"message": "boom"})
        m = re.fullmatch(_REPO + r"/(?P<rest>.*)", path)
        if m is None or m["repo"] not in self.repos:
            return httpx.Response(404, json={})
        world = self.repos[m["repo"]]
        rest = m["rest"]
        if request.method == "POST":
            sm = re.fullmatch(r"statuses/(?P<sha>.+)", rest)
            if sm is None:
                return httpx.Response(404, json={})
            self.statuses.append(json.loads(request.content))
            self.status_targets.append((m["repo"], unquote(sm["sha"])))
            return httpx.Response(201, json={})
        if rest.startswith("branches/"):
            if unquote(rest[len("branches/") :]) != "main" or world.get("sha") is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"commit": {"id": world["sha"]}})
        if rest.startswith("git/trees/"):
            tree = [{"path": p, "type": "blob", "sha": "x"} for p in sorted(world["tree"])]
            return httpx.Response(200, json={"tree": tree, "total_count": len(tree)})
        if rest.startswith("raw/"):
            text = world["tree"].get(unquote(rest[len("raw/") :]))
            if text is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, content=text.encode("utf-8"))
        if rest.startswith("compare/"):
            return httpx.Response(200, json={"files": world.get("compare", [])})
        return httpx.Response(404, json={})


def grist_transport(tables: dict[str, list[Any]]) -> httpx.MockTransport:
    """Serve ``/api/docs/<doc>/tables/<table>/records``. A row is a key string or a fields dict.

    The transport has a ``requests`` list of every request seen.
    """
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        m = re.fullmatch(r"/api/docs/[^/]+/tables/(?P<table>[^/]+)/records", request.url.path)
        if m is None or m["table"] not in tables:
            return httpx.Response(404, json={})
        rows = [{"key": r} if isinstance(r, str) else r for r in tables[m["table"]]]
        return httpx.Response(200, json={"records": [{"id": i, "fields": f} for i, f in enumerate(rows, 1)]})

    transport = httpx.MockTransport(handle)
    transport.requests = seen  # type: ignore[attr-defined]
    return transport
