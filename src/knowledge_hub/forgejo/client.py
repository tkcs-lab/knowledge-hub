"""Forgejo client: read methods for the indexer and the write guard for promotion.

Reads return Forgejo's JSON shapes as plain dicts and lists (the shapes ``indexer/trust.py`` consumes).
Errors carry a fixed text with the method, path and status only: never the token, never the body.
Every write added later must call ``_guard_ref`` on its target ref before sending anything.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

BOT_REF_PREFIX = "bot/promote/"
_PAGE = 50
_TREE_PAGE = 1000
_MAX_PAGES = 1000


class ForgejoError(Exception):
    """A Forgejo call failed. The message never contains the token."""


class ForgejoRefused(ForgejoError):
    """A write was refused locally because it targets a ref outside ``bot/promote/``."""


def _guard_ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref.startswith(BOT_REF_PREFIX) or len(ref) == len(BOT_REF_PREFIX):
        raise ForgejoRefused("write refused: ref must start with bot/promote/")
    if ".." in ref or "//" in ref:
        raise ForgejoRefused("write refused: ref must start with bot/promote/")
    return ref


def _q(part: str) -> str:
    return quote(part, safe="")


class ForgejoClient:
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
        return f"/repos/{_q(self.org)}/{_q(repo)}"

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            resp = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ForgejoError(f"GET {path} failed: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise ForgejoError(f"GET {path} failed: HTTP {resp.status_code}")
        return resp

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._get(path, params)
        try:
            return resp.json()
        except ValueError:
            raise ForgejoError(f"GET {path} failed: invalid JSON") from None

    # -- reads ---------------------------------------------------------------------------------

    async def head_sha(self, repo: str, branch: str) -> str:
        data = await self._get_json(f"{self._repo(repo)}/branches/{quote(branch, safe='/')}")
        try:
            return data["commit"]["id"]
        except (KeyError, TypeError):
            raise ForgejoError("branch response has no commit id") from None

    async def list_tree(self, repo: str, sha: str) -> list[dict[str, Any]]:
        """All entries of the recursive tree at ``sha`` (``path``, ``type``, ``sha``, ``size``)."""
        entries: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            data = await self._get_json(
                f"{self._repo(repo)}/git/trees/{_q(sha)}",
                {"recursive": "true", "per_page": _TREE_PAGE, "page": page},
            )
            batch = data.get("tree") or []
            entries.extend(batch)
            total = data.get("total_count")
            if not batch or (total is not None and len(entries) >= total) or (
                total is None and len(batch) < _TREE_PAGE
            ):
                break
        return entries

    async def get_raw(self, repo: str, path: str, sha: str) -> str:
        resp = await self._get(f"{self._repo(repo)}/raw/{quote(path, safe='/')}", {"ref": sha})
        return resp.content.decode("utf-8")

    async def commits_first_parent(
        self, repo: str, branch: str, since_sha: str | None = None
    ) -> list[dict[str, Any]]:
        """First-parent chain of ``branch``, newest first, stopping before ``since_sha``.

        Forgejo has no first-parent filter, so the chain is followed through ``parents[0]`` over the
        paged commit list. When ``since_sha`` is not on the chain the whole chain is returned.
        """
        head = await self.head_sha(repo, branch)
        by_sha: dict[str, dict[str, Any]] = {}
        chain: list[dict[str, Any]] = []
        want: str | None = head
        exhausted = False
        page = 0
        while want is not None and want != since_sha:
            if want in by_sha:
                commit = by_sha[want]
                chain.append(commit)
                parents = commit.get("parents") or []
                want = parents[0]["sha"] if parents else None
                continue
            if exhausted or page >= _MAX_PAGES:
                raise ForgejoError("commit list ended before the first-parent chain did")
            page += 1
            batch = await self._get_json(
                f"{self._repo(repo)}/commits",
                {"sha": head, "stat": "false", "verification": "false", "files": "false",
                 "page": page, "limit": _PAGE},
            )
            for commit in batch:
                by_sha[commit["sha"]] = commit
            exhausted = len(batch) < _PAGE
        return chain

    async def pull_for_commit(self, repo: str, sha: str) -> dict[str, Any] | None:
        """The pull request that merged ``sha``; ``None`` when Forgejo has none (HTTP 404)."""
        path = f"{self._repo(repo)}/commits/{_q(sha)}/pull"
        try:
            resp = await self._http.get(path)
        except httpx.HTTPError as exc:
            raise ForgejoError(f"GET {path} failed: {type(exc).__name__}") from None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ForgejoError(f"GET {path} failed: HTTP {resp.status_code}")
        return resp.json()

    async def pull(self, repo: str, number: int) -> dict[str, Any]:
        return await self._get_json(f"{self._repo(repo)}/pulls/{int(number)}")

    async def pull_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = await self._get_json(
                f"{self._repo(repo)}/pulls/{int(number)}/reviews", {"page": page, "limit": _PAGE}
            )
            out.extend(batch)
            if len(batch) < _PAGE:
                break
        return out

    async def changed_files(self, repo: str, base: str, head: str) -> list[str]:
        """Paths changed between ``base`` and ``head`` (compare endpoint)."""
        data = await self._get_json(f"{self._repo(repo)}/compare/{_q(base)}...{_q(head)}")
        return [f["filename"] for f in data.get("files") or []]
