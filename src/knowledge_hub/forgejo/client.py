"""Forgejo client: read methods for the indexer and the write guard for promotion.

Reads return Forgejo's JSON shapes as plain dicts and lists (the shapes ``indexer/trust.py`` consumes).
Errors carry a fixed text with the method, path and status only: never the token, never the body.
Every write calls ``_guard_ref`` on its target ref before sending anything; status posts are guarded by
``_guard_context`` instead of a ref.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx

BOT_REF_PREFIX = "bot/promote/"
_PAGE = 50
_TREE_PAGE = 1000
_MAX_PAGES = 1000
STATUS_CONTEXTS = ("kh/validate", "kh/domain-owners")
STATUS_STATES = ("success", "failure", "error", "pending")


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


def _guard_context(context: str, state: str) -> None:
    if context not in STATUS_CONTEXTS or state not in STATUS_STATES:
        raise ForgejoRefused("write refused: not a kh status")


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

    async def compare_files(self, repo: str, base: str, head: str) -> list[dict[str, Any]]:
        """Files changed between ``base`` and ``head``: ``filename``, ``status``, ``previous_filename``."""
        data = await self._get_json(f"{self._repo(repo)}/compare/{_q(base)}...{_q(head)}")
        out: list[dict[str, Any]] = []
        for f in data.get("files") or []:
            item: dict[str, Any] = {"filename": f["filename"], "status": f.get("status")}
            if f.get("previous_filename"):
                item["previous_filename"] = f["previous_filename"]
            out.append(item)
        return out

    async def head_sha_or_none(self, repo: str, branch: str) -> str | None:
        """Head sha of ``branch``; HTTP 404 means the branch does not exist."""
        url = f"{self._repo(repo)}/branches/{quote(branch, safe='/')}"
        try:
            resp = await self._http.get(url)
        except httpx.HTTPError as exc:
            raise ForgejoError(f"GET {url} failed: {type(exc).__name__}") from None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ForgejoError(f"GET {url} failed: HTTP {resp.status_code}")
        try:
            return resp.json()["commit"]["id"]
        except (ValueError, KeyError, TypeError):
            raise ForgejoError("branch response has no commit id") from None

    async def team_id(self, name: str) -> int | None:
        """Id of the org team called ``name``, or ``None``."""
        for page in range(1, _MAX_PAGES + 1):
            data = await self._get_json(f"/orgs/{_q(self.org)}/teams", {"page": page, "limit": _PAGE})
            for team in data:
                if team.get("name") == name:
                    return team.get("id")
            if len(data) < _PAGE:
                break
        return None

    async def is_team_member(self, team_id: int, login: str) -> bool:
        url = f"/teams/{team_id}/members/{_q(login)}"
        try:
            resp = await self._http.get(url)
        except httpx.HTTPError as exc:
            raise ForgejoError(f"GET {url} failed: {type(exc).__name__}") from None
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 404:
            return False
        raise ForgejoError(f"GET {url} failed: HTTP {resp.status_code}")

    async def path_exists(self, repo: str, path: str, ref: str) -> bool:
        """Whether ``path`` exists at ``ref`` (a read: not guarded). HTTP 404 means no."""
        url = f"{self._repo(repo)}/contents/{quote(path, safe='/')}"
        try:
            resp = await self._http.get(url, params={"ref": ref})
        except httpx.HTTPError as exc:
            raise ForgejoError(f"GET {url} failed: {type(exc).__name__}") from None
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise ForgejoError(f"GET {url} failed: HTTP {resp.status_code}")
        return True

    # -- writes: every one calls ``_guard_ref`` on the branch it writes before sending ---------

    async def _send(self, method: str, path: str, body: dict[str, Any]) -> Any:
        try:
            resp = await self._http.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise ForgejoError(f"{method} {path} failed: {type(exc).__name__}") from None
        if resp.status_code not in (200, 201):
            raise ForgejoError(f"{method} {path} failed: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise ForgejoError(f"{method} {path} failed: invalid JSON") from None

    async def post_status(self, repo: str, sha: str, context: str, state: str, description: str) -> None:
        _guard_context(context, state)
        await self._send(
            "POST",
            f"{self._repo(repo)}/statuses/{_q(sha)}",
            {"context": context, "state": state, "description": description[:140]},
        )

    async def create_branch(self, repo: str, name: str, from_sha: str) -> None:
        _guard_ref(name)
        await self._send(
            "POST", f"{self._repo(repo)}/branches", {"new_branch_name": name, "old_ref_name": from_sha}
        )

    async def put_file(self, repo: str, branch: str, path: str, content: str, message: str) -> None:
        """Create ``path`` on ``branch`` or, when it exists there, replace it."""
        _guard_ref(branch)
        url = f"{self._repo(repo)}/contents/{quote(path, safe='/')}"
        body: dict[str, Any] = {
            "branch": branch,
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        existing = await self._existing_blob_sha(url, branch)
        if existing is None:
            await self._send("POST", url, body)
        else:
            await self._send("PUT", url, {**body, "sha": existing})

    async def _existing_blob_sha(self, url: str, branch: str) -> str | None:
        try:
            resp = await self._http.get(url, params={"ref": branch})
        except httpx.HTTPError as exc:
            raise ForgejoError(f"GET {url} failed: {type(exc).__name__}") from None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ForgejoError(f"GET {url} failed: HTTP {resp.status_code}")
        try:
            return resp.json()["sha"]
        except (ValueError, KeyError, TypeError):
            raise ForgejoError(f"GET {url} failed: invalid JSON") from None

    async def _label_ids(self, repo: str, names: list[str]) -> list[int]:
        ids: dict[str, int] = {}
        for path in (f"{self._repo(repo)}/labels", f"/orgs/{_q(self.org)}/labels"):
            for page in range(1, _MAX_PAGES + 1):
                try:
                    batch = await self._get_json(path, {"page": page, "limit": _PAGE})
                except ForgejoError:
                    if path.startswith("/orgs/"):
                        break  # labels not readable at org level: repo labels only
                    raise
                for label in batch:
                    ids.setdefault(label["name"], label["id"])
                if len(batch) < _PAGE:
                    break
        missing = [n for n in names if n not in ids]
        if missing:
            raise ForgejoError("label not found: " + ", ".join(missing))
        return [ids[n] for n in names]

    async def open_pull(
        self, repo: str, head: str, base: str, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        _guard_ref(head)
        if base != "main":
            raise ForgejoRefused("write refused: pull request base must be main")
        label_ids = await self._label_ids(repo, labels) if labels else []
        return await self._send(
            "POST",
            f"{self._repo(repo)}/pulls",
            {"head": head, "base": base, "title": title, "body": body, "labels": label_ids},
        )
