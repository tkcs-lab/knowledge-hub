"""Forgejo admin client for ``kh setup``: teams, repos, collaborators, protection, org and repo hooks.

Same error handling as the reader: a fixed text with method, path and status, never the token or a body.
Every method returns plain dicts and lists. ``kh serve`` never builds this client.
"""

from __future__ import annotations

from typing import Any

import httpx

from knowledge_hub.forgejo.reader import MAX_PAGES, PAGE, ForgejoError, _Http, q

OWNERS_TEAM = "Owners"


class ForgejoAdmin(_Http):
    def __init__(
        self,
        base_url: str,
        token: str,
        org: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(base_url, token, org, transport=transport)

    # -- helpers ---------------------------------------------------------------------------------

    async def _list(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._json("GET", path, params={"page": page, "limit": PAGE})
            if not isinstance(data, list):
                raise ForgejoError(f"GET {path} failed: response is not a list")
            out.extend(data)
            if len(data) < PAGE:
                break
        return out

    async def _obj(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200,),
        missing: bool = False,
    ) -> dict[str, Any] | None:
        data = await self._json(method, path, body=body, ok=ok, missing=missing)
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ForgejoError(f"{method} {path} failed: response is not an object")
        return data

    async def _obj_required(
        self, method: str, path: str, *, body: dict[str, Any] | None = None, ok: tuple[int, ...] = (200,)
    ) -> dict[str, Any]:
        data = await self._obj(method, path, body=body, ok=ok)
        assert data is not None
        return data

    # -- teams -----------------------------------------------------------------------------------

    async def org_teams(self) -> list[dict[str, Any]]:
        return await self._list(f"/orgs/{q(self.org)}/teams")

    async def create_team(
        self, name: str, permission: str, description: str = "", **fields: Any
    ) -> dict[str, Any]:
        body = {"name": name, "permission": permission, "description": description, **fields}
        return await self._obj_required("POST", f"/orgs/{q(self.org)}/teams", body=body, ok=(201,))

    async def edit_team(self, team_id: int, **fields: Any) -> dict[str, Any]:
        return await self._obj_required("PATCH", f"/teams/{team_id}", body=fields)

    async def team_members(self, team_id: int) -> list[dict[str, Any]]:
        return await self._list(f"/teams/{team_id}/members")

    async def add_team_member(self, team_id: int, login: str) -> None:
        await self._request("PUT", f"/teams/{team_id}/members/{q(login)}", ok=(204,))

    async def remove_team_member(self, team_id: int, login: str) -> None:
        await self._request("DELETE", f"/teams/{team_id}/members/{q(login)}", ok=(204,))

    async def team_repos(self, team_id: int) -> list[dict[str, Any]]:
        return await self._list(f"/teams/{team_id}/repos")

    async def add_team_repo(self, team_id: int, repo: str) -> None:
        await self._request("PUT", f"/teams/{team_id}/repos/{q(self.org)}/{q(repo)}", ok=(204,))

    # -- repositories ----------------------------------------------------------------------------

    async def repos(self) -> list[dict[str, Any]]:
        return await self._list(f"/orgs/{q(self.org)}/repos")

    async def get_repo(self, repo: str) -> dict[str, Any] | None:
        return await self._obj("GET", self._repo(repo), missing=True)

    async def create_repo(self, repo: str) -> dict[str, Any]:
        body = {"name": repo, "private": True, "default_branch": "main", "auto_init": True}
        return await self._obj_required("POST", f"/orgs/{q(self.org)}/repos", body=body, ok=(201,))

    async def edit_repo(self, repo: str, **fields: Any) -> dict[str, Any]:
        return await self._obj_required("PATCH", self._repo(repo), body=fields)

    async def collaborators(self, repo: str) -> list[dict[str, Any]]:
        return await self._list(f"{self._repo(repo)}/collaborators")

    async def put_collaborator(self, repo: str, login: str, permission: str) -> None:
        await self._request(
            "PUT", f"{self._repo(repo)}/collaborators/{q(login)}", body={"permission": permission}, ok=(204,)
        )

    # -- branch protection -----------------------------------------------------------------------

    async def protection(self, repo: str) -> dict[str, Any] | None:
        """The rule named ``main``, or None."""
        return await self._obj("GET", f"{self._repo(repo)}/branch_protections/main", missing=True)

    async def put_protection(self, repo: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create the rule, or edit it when one with the same ``rule_name`` exists."""
        name = body.get("rule_name")
        if not isinstance(name, str) or not name:
            raise ForgejoError("protection body has no rule_name")
        existing = await self._obj("GET", f"{self._repo(repo)}/branch_protections/{q(name)}", missing=True)
        if existing is None:
            return await self._obj_required(
                "POST", f"{self._repo(repo)}/branch_protections", body=body, ok=(201,)
            )
        return await self._obj_required(
            "PATCH", f"{self._repo(repo)}/branch_protections/{q(name)}", body=body
        )

    # -- org hooks -------------------------------------------------------------------------------

    async def org_hooks(self) -> list[dict[str, Any]]:
        return await self._list(f"/orgs/{q(self.org)}/hooks")

    async def create_org_hook(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._obj_required("POST", f"/orgs/{q(self.org)}/hooks", body=body, ok=(201,))

    async def edit_org_hook(self, hook_id: int, body: dict[str, Any]) -> dict[str, Any]:
        return await self._obj_required("PATCH", f"/orgs/{q(self.org)}/hooks/{hook_id}", body=body)

    # -- repo hooks ------------------------------------------------------------------------------

    async def repo_hooks(self, repo: str) -> list[dict[str, Any]]:
        return await self._list(f"{self._repo(repo)}/hooks")

    async def create_repo_hook(self, repo: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._obj_required("POST", f"{self._repo(repo)}/hooks", body=body, ok=(201,))

    async def edit_repo_hook(self, repo: str, hook_id: int, body: dict[str, Any]) -> dict[str, Any]:
        return await self._obj_required("PATCH", f"{self._repo(repo)}/hooks/{hook_id}", body=body)

    async def delete_repo_hook(self, repo: str, hook_id: int) -> None:
        await self._request("DELETE", f"{self._repo(repo)}/hooks/{hook_id}", ok=(204,))

    # -- identity --------------------------------------------------------------------------------

    async def whoami(self, token: str | None = None) -> dict[str, Any]:
        """``GET /user``; with ``token`` the call is made as that account instead of the admin."""
        headers = {"Authorization": f"token {token}"} if token is not None else None
        try:
            resp = await self._http.request("GET", "/user", headers=headers)
        except httpx.HTTPError as exc:
            raise ForgejoError(f"GET /user failed: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise ForgejoError(f"GET /user failed: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise ForgejoError("GET /user failed: invalid JSON") from None
        if not isinstance(data, dict):
            raise ForgejoError("GET /user failed: response is not an object")
        return data

    async def user_repo_permission(self, repo: str, login: str) -> dict[str, Any]:
        return await self._obj_required("GET", f"{self._repo(repo)}/collaborators/{q(login)}/permission")

    async def org_owners(self) -> list[dict[str, Any]]:
        """Members of the org's Owners team (people who can bypass protection)."""
        for team in await self.org_teams():
            if team.get("name") == OWNERS_TEAM:
                return await self.team_members(int(team["id"]))
        return []
