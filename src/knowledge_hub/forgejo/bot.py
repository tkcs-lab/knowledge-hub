"""Write client of the bot account: it can only touch ``bot/`` branches and open pull requests to main."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from knowledge_hub.forgejo.reader import MAX_PAGES, PAGE, ForgejoError, ForgejoRefused, _Http

BOT_REF_PREFIX = "bot/"


def _guard_branch(name: object) -> str:
    msg = "write refused: branch must start with bot/"
    if not isinstance(name, str) or not name.startswith(BOT_REF_PREFIX) or len(name) <= len(BOT_REF_PREFIX):
        raise ForgejoRefused(msg)
    if ".." in name or "//" in name:
        raise ForgejoRefused(msg)
    return name


class ForgejoBot(_Http):
    async def blob_sha(self, repo: str, path: str, ref: str) -> str | None:
        """Git blob sha of ``path`` at ``ref``; ``None`` when the file does not exist."""
        data = await self._json(
            "GET", f"{self._repo(repo)}/contents/{quote(path, safe='/')}", params={"ref": ref}, missing=True
        )
        if data is None:
            return None
        try:
            return data["sha"]
        except (KeyError, TypeError):
            raise ForgejoError("contents response has no sha") from None

    async def create_branch(self, repo: str, name: str, from_sha: str) -> None:
        _guard_branch(name)
        await self._json(
            "POST",
            f"{self._repo(repo)}/branches",
            body={"new_branch_name": name, "old_ref_name": from_sha},
            ok=(200, 201),
        )

    async def put_file(
        self, repo: str, branch: str, path: str, content: str, message: str, prev_blob: str | None
    ) -> dict[str, Any]:
        """Create ``path`` on ``branch`` (``prev_blob`` None) or replace it; returns the commit."""
        _guard_branch(branch)
        body: dict[str, Any] = {
            "branch": branch,
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if prev_blob is not None:
            body["sha"] = prev_blob
        resp = await self._json(
            "POST" if prev_blob is None else "PUT",
            f"{self._repo(repo)}/contents/{quote(path, safe='/')}",
            body=body,
            ok=(200, 201),
        )
        commit = resp.get("commit") if isinstance(resp, dict) else None
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not sha:
            raise ForgejoError("commit response has no sha")
        return {"sha": sha, "parents": [p["sha"] for p in commit.get("parents") or []]}

    async def open_pull(self, repo: str, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        _guard_branch(head)
        if base != "main":
            raise ForgejoRefused("write refused: pull request base must be main")
        data = await self._json(
            "POST",
            f"{self._repo(repo)}/pulls",
            body={"head": head, "base": base, "title": title, "body": body},
            ok=(200, 201),
        )
        if not isinstance(data, dict):
            raise ForgejoError("pull response is not an object")
        return data

    async def list_open_pulls(self, repo: str, author: str) -> list[dict[str, Any]]:
        """Open pull requests of ``repo`` opened by the login ``author``."""
        out: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            batch = await self._json(
                "GET",
                f"{self._repo(repo)}/pulls",
                params={"state": "open", "page": page, "limit": PAGE},
            )
            if not isinstance(batch, list):
                raise ForgejoError("pulls response is not a list")
            for pull in batch:
                user = pull.get("user")
                if isinstance(user, dict) and user.get("login") == author:
                    out.append(pull)
            if len(batch) < PAGE:
                break
        return out
