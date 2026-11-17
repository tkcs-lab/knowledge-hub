"""Read-only Forgejo client for the indexer, and the plumbing the bot client shares.

Errors carry a fixed text with the method, path and status only: never the token, never the body.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

PAGE = 50
TREE_PAGE = 1000
MAX_PAGES = 1000


class ForgejoError(Exception):
    """A Forgejo call failed. The message never contains the token."""


class ForgejoRefused(ForgejoError):
    """A write was refused locally by the guard before anything was sent."""


def q(part: str) -> str:
    return quote(part, safe="")


class _Http:
    def __init__(
        self,
        base_url: str,
        token: str,
        org: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.org = org
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"Authorization": f"token {token}", "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _repo(self, repo: str) -> str:
        return f"/repos/{q(self.org)}/{q(repo)}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200,),
        missing: bool = False,
    ) -> httpx.Response | None:
        """Send one request; ``missing`` turns HTTP 404 into ``None``."""
        try:
            resp = await self._http.request(method, path, params=params, json=body)
        except httpx.HTTPError as exc:
            raise ForgejoError(f"{method} {path} failed: {type(exc).__name__}") from None
        if missing and resp.status_code == 404:
            return None
        if resp.status_code not in ok:
            raise ForgejoError(f"{method} {path} failed: HTTP {resp.status_code}")
        return resp

    async def _json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200,),
        missing: bool = False,
    ) -> Any:
        resp = await self._request(method, path, params=params, body=body, ok=ok, missing=missing)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            raise ForgejoError(f"{method} {path} failed: invalid JSON") from None

    async def head_sha(self, repo: str, branch: str) -> str:
        data = await self._json("GET", f"{self._repo(repo)}/branches/{quote(branch, safe='/')}")
        try:
            sha = data["commit"]["id"]
        except (KeyError, TypeError):
            raise ForgejoError("branch response has no commit id") from None
        if not isinstance(sha, str) or not sha:
            raise ForgejoError("branch response has no commit id")
        return sha


class ForgejoReader(_Http):
    async def list_tree(self, repo: str, sha: str) -> list[dict[str, Any]]:
        """All entries of the recursive tree at ``sha`` (``path``, ``sha``, ``type``, ``size``)."""
        entries: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._json(
                "GET",
                f"{self._repo(repo)}/git/trees/{q(sha)}",
                params={"recursive": "true", "per_page": TREE_PAGE, "page": page},
            )
            if not isinstance(data, dict):
                raise ForgejoError("tree response is not an object")
            batch = data.get("tree") or []
            entries.extend(
                {"path": e["path"], "sha": e["sha"], "type": e["type"], "size": e.get("size", 0)}
                for e in batch
            )
            total = data.get("total_count")
            if not batch or (total is not None and len(entries) >= total) or (
                total is None and len(batch) < TREE_PAGE
            ):
                break
        return entries

    async def get_raw(self, repo: str, path: str, sha: str) -> str:
        resp = await self._request(
            "GET", f"{self._repo(repo)}/raw/{quote(path, safe='/')}", params={"ref": sha}
        )
        assert resp is not None
        try:
            return resp.content.decode("utf-8")
        except UnicodeDecodeError:
            raise ForgejoError(f"GET raw {path} failed: not UTF-8") from None

    async def is_ancestor(self, repo: str, base: str, head: str) -> bool:
        """Whether ``base`` is an ancestor of (or equal to) ``head``; any 404 or error answers False.

        The compare API lists the commits of the right side that the left side lacks, so comparing
        ``head...base`` is empty exactly when ``base`` is reachable from ``head``.
        """
        try:
            data = await self._json("GET", f"{self._repo(repo)}/compare/{q(head)}...{q(base)}", missing=True)
        except ForgejoError:
            return False
        if not isinstance(data, dict):
            return False
        total = data.get("total_commits")
        if not isinstance(total, int):
            commits = data.get("commits")
            if not isinstance(commits, list):
                return False
            total = len(commits)
        return total == 0
