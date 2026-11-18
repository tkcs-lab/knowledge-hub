"""A stateful in-memory admin Forgejo behind ``httpx.MockTransport``, for ``ForgejoAdmin`` and ``kh setup``.

Behaviours that follow the plan (3.7, research r-20261117-1 and -2):
- branch protection is stored by ``rule_name``;
- a team in ``merge_whitelist_teams`` that has no read access to the repo is silently dropped on write
  (Forgejo does this in ``protected_branch.go``), a team name that does not exist answers 422;
- a team's repo access is its ``permission`` once the repo is added to it (or ``includes_all_repositories``).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

import httpx

RANK = {"none": 0, "read": 1, "write": 2, "admin": 3, "owner": 4}
PERMISSIONS = ("read", "write", "admin")


def _page(items: list[Any], params: httpx.QueryParams) -> list[Any]:
    limit = int(params.get("limit", 50))
    page = int(params.get("page", 1))
    return items[(page - 1) * limit : page * limit]


class FakeForgejoAdmin:
    """Use ``fake.transport`` as ``ForgejoAdmin``'s ``transport=``; ``fake.requests`` logs (method, path)."""

    def __init__(self, org: str = "kh", token: str = "admin-tok", admin_login: str = "kh-admin") -> None:
        self.org = org
        self.tokens: dict[str, str] = {token: admin_login}  # token -> login
        self.site_admins: set[str] = {admin_login}
        self.users: set[str] = {admin_login}
        self.teams: dict[int, dict[str, Any]] = {}
        self.team_members: dict[int, list[str]] = {}
        self.team_repos: dict[int, list[str]] = {}
        self.repos: dict[str, dict[str, Any]] = {}
        self.collabs: dict[str, dict[str, str]] = {}  # repo -> login -> permission
        self.protections: dict[str, dict[str, dict[str, Any]]] = {}  # repo -> rule_name -> rule
        self.hooks: dict[int, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[dict[str, Any]] = []
        self._next_id = 0
        self._failures: list[tuple[str, int]] = []
        self.transport = httpx.MockTransport(self._handle)
        self.add_team("Owners", "owner")

    # -- test setup ------------------------------------------------------------------------------

    def add_user(self, login: str, token: str | None = None, *, site_admin: bool = False) -> None:
        self.users.add(login)
        if token is not None:
            self.tokens[token] = login
        if site_admin:
            self.site_admins.add(login)

    def add_team(self, name: str, permission: str = "read", members: list[str] | None = None) -> int:
        self._next_id += 1
        tid = self._next_id
        self.teams[tid] = {
            "id": tid, "name": name, "description": "", "permission": permission,
            "includes_all_repositories": False, "can_create_org_repo": False, "units_map": {},
        }
        self.team_members[tid] = list(members or [])
        self.team_repos[tid] = []
        for login in members or []:
            self.users.add(login)
        return tid

    def team_id(self, name: str) -> int:
        return next(t["id"] for t in self.teams.values() if t["name"] == name)

    def add_repo(self, name: str) -> None:
        self.repos[name] = self._new_repo(name)
        self.collabs.setdefault(name, {})
        self.protections.setdefault(name, {})

    def fail_next(self, path_part: str, status: int) -> None:
        self._failures.append((path_part, status))

    # -- model -----------------------------------------------------------------------------------

    def _new_repo(self, name: str) -> dict[str, Any]:
        return {
            "name": name, "full_name": f"{self.org}/{name}", "private": True, "default_branch": "main",
            "has_actions": True, "default_delete_branch_after_merge": False, "empty": False,
            "archived": False,
        }

    def _team_access(self, tid: int, repo: str) -> str:
        team = self.teams[tid]
        if team["permission"] == "owner" or team["includes_all_repositories"] or repo in self.team_repos[tid]:
            return team["permission"]
        return "none"

    def _repo_permission(self, repo: str, login: str) -> str:
        if login in self.site_admins:
            return "admin"
        if login in self.team_members[self.team_id("Owners")]:
            return "owner"
        best = self.collabs.get(repo, {}).get(login, "none")
        for tid, members in self.team_members.items():
            if login in members and RANK[self._team_access(tid, repo)] > RANK[best]:
                best = self._team_access(tid, repo)
        return best

    def _user(self, login: str) -> dict[str, Any]:
        return {"login": login, "id": abs(hash(login)) % 10**6, "is_admin": login in self.site_admins}

    # -- request handling ------------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        method = request.method
        self.requests.append((method, path))
        try:
            body = json.loads(request.content) if request.content else {}
        except ValueError:
            body = {}
        if body:
            self.bodies.append(body)
        for i, (part, status) in enumerate(self._failures):
            if part in path:
                del self._failures[i]
                return httpx.Response(status, json={"message": "injected"})
        auth = request.headers.get("Authorization", "")
        login = self.tokens.get(auth.removeprefix("token ")) if auth.startswith("token ") else None
        if login is None:
            return httpx.Response(401, json={"message": "unauthorized"})
        if path == "/api/v1/user" and method == "GET":
            return httpx.Response(200, json=self._user(login))
        if login not in self.site_admins:
            return httpx.Response(403, json={"message": "admin required"})
        rel = path.removeprefix("/api/v1")
        for pattern, handler in self._routes():
            m = re.fullmatch(pattern[1], rel)
            if m and pattern[0] == method:
                return handler(m, body, request.url.params)
        return httpx.Response(404, json={"message": "not found"})

    def _routes(self):
        o = re.escape(self.org)
        r = r"/repos/" + o + r"/([^/]+)"
        return [
            (("GET", rf"/orgs/{o}/teams"), self._teams_list),
            (("POST", rf"/orgs/{o}/teams"), self._team_create),
            (("PATCH", r"/teams/(\d+)"), self._team_edit),
            (("GET", r"/teams/(\d+)/members"), self._members_list),
            (("PUT", r"/teams/(\d+)/members/([^/]+)"), self._member_add),
            (("DELETE", r"/teams/(\d+)/members/([^/]+)"), self._member_remove),
            (("GET", r"/teams/(\d+)/repos"), self._team_repos_list),
            (("PUT", rf"/teams/(\d+)/repos/{o}/([^/]+)"), self._team_repo_add),
            (("GET", rf"/orgs/{o}/repos"), self._repos_list),
            (("POST", rf"/orgs/{o}/repos"), self._repo_create),
            (("GET", r), self._repo_get),
            (("PATCH", r), self._repo_edit),
            (("GET", r + r"/collaborators"), self._collabs_list),
            (("PUT", r + r"/collaborators/([^/]+)"), self._collab_put),
            (("GET", r + r"/collaborators/([^/]+)/permission"), self._collab_permission),
            (("GET", r + r"/branch_protections/([^/]+)"), self._prot_get),
            (("POST", r + r"/branch_protections"), self._prot_create),
            (("PATCH", r + r"/branch_protections/([^/]+)"), self._prot_edit),
            (("GET", rf"/orgs/{o}/hooks"), self._hooks_list),
            (("POST", rf"/orgs/{o}/hooks"), self._hook_create),
            (("PATCH", rf"/orgs/{o}/hooks/(\d+)"), self._hook_edit),
        ]

    # teams

    def _teams_list(self, m, body, params):
        return httpx.Response(200, json=_page([dict(t) for t in self.teams.values()], params))

    def _team_create(self, m, body, params):
        name, perm = body.get("name"), body.get("permission")
        if not name or perm not in PERMISSIONS:
            return httpx.Response(422, json={"message": "invalid team"})
        if any(t["name"] == name for t in self.teams.values()):
            return httpx.Response(409, json={"message": "team exists"})
        tid = self.add_team(name, perm)
        self.teams[tid].update({k: v for k, v in body.items() if k in self.teams[tid] and k != "id"})
        return httpx.Response(201, json=dict(self.teams[tid]))

    def _team_edit(self, m, body, params):
        tid = int(m[1])
        if tid not in self.teams:
            return httpx.Response(404, json={"message": "no team"})
        if "permission" in body and body["permission"] not in PERMISSIONS:
            return httpx.Response(422, json={"message": "invalid permission"})
        self.teams[tid].update({k: v for k, v in body.items() if k in self.teams[tid] and k != "id"})
        return httpx.Response(200, json=dict(self.teams[tid]))

    def _members_list(self, m, body, params):
        tid = int(m[1])
        if tid not in self.teams:
            return httpx.Response(404, json={"message": "no team"})
        return httpx.Response(200, json=_page([self._user(x) for x in self.team_members[tid]], params))

    def _member_add(self, m, body, params):
        tid, login = int(m[1]), m[2]
        if tid not in self.teams or login not in self.users:
            return httpx.Response(404, json={"message": "not found"})
        if login not in self.team_members[tid]:
            self.team_members[tid].append(login)
        return httpx.Response(204)

    def _member_remove(self, m, body, params):
        tid, login = int(m[1]), m[2]
        if tid not in self.teams or login not in self.team_members[tid]:
            return httpx.Response(404, json={"message": "not found"})
        self.team_members[tid].remove(login)
        return httpx.Response(204)

    def _team_repos_list(self, m, body, params):
        tid = int(m[1])
        if tid not in self.teams:
            return httpx.Response(404, json={"message": "no team"})
        names = list(self.repos) if self.teams[tid]["includes_all_repositories"] else self.team_repos[tid]
        return httpx.Response(200, json=_page([dict(self.repos[n]) for n in names], params))

    def _team_repo_add(self, m, body, params):
        tid, repo = int(m[1]), m[2]
        if tid not in self.teams or repo not in self.repos:
            return httpx.Response(404, json={"message": "not found"})
        if repo not in self.team_repos[tid]:
            self.team_repos[tid].append(repo)
        return httpx.Response(204)

    # repos

    def _repos_list(self, m, body, params):
        return httpx.Response(200, json=_page([dict(r) for r in self.repos.values()], params))

    def _repo_create(self, m, body, params):
        name = body.get("name")
        if not name:
            return httpx.Response(422, json={"message": "name required"})
        if name in self.repos:
            return httpx.Response(409, json={"message": "repo exists"})
        self.add_repo(name)
        self.repos[name]["private"] = bool(body.get("private", False))
        self.repos[name]["default_branch"] = body.get("default_branch") or "main"
        self.repos[name]["empty"] = not body.get("auto_init", False)
        return httpx.Response(201, json=dict(self.repos[name]))

    def _repo_get(self, m, body, params):
        repo = self.repos.get(m[1])
        if repo is None:
            return httpx.Response(404, json={"message": "no repo"})
        return httpx.Response(200, json=dict(repo))

    def _repo_edit(self, m, body, params):
        repo = self.repos.get(m[1])
        if repo is None:
            return httpx.Response(404, json={"message": "no repo"})
        repo.update({k: v for k, v in body.items() if k in repo and k not in ("name", "full_name")})
        return httpx.Response(200, json=dict(repo))

    def _collabs_list(self, m, body, params):
        if m[1] not in self.repos:
            return httpx.Response(404, json={"message": "no repo"})
        return httpx.Response(200, json=_page([self._user(x) for x in self.collabs[m[1]]], params))

    def _collab_put(self, m, body, params):
        repo, login = m[1], m[2]
        if repo not in self.repos or login not in self.users:
            return httpx.Response(404, json={"message": "not found"})
        perm = body.get("permission", "write")
        if perm not in PERMISSIONS:
            return httpx.Response(422, json={"message": "invalid permission"})
        self.collabs[repo][login] = perm
        return httpx.Response(204)

    def _collab_permission(self, m, body, params):
        repo, login = m[1], m[2]
        if repo not in self.repos or login not in self.users:
            return httpx.Response(404, json={"message": "not found"})
        perm = self._repo_permission(repo, login)
        return httpx.Response(200, json={"permission": perm, "role_name": perm, "user": self._user(login)})

    # protection

    def _clean_protection(self, repo: str, body: dict[str, Any]):
        """Apply the whitelist rules; a bad team name is an error response, a no-read team is dropped."""
        rule = dict(body)
        teams = rule.get("merge_whitelist_teams") or []
        kept = []
        for name in teams:
            tid = next((t["id"] for t in self.teams.values() if t["name"] == name), None)
            if tid is None:
                return None, httpx.Response(422, json={"message": f"team does not exist [name: {name}]"})
            if RANK[self._team_access(tid, repo)] >= RANK["read"]:
                kept.append(name)
        if "merge_whitelist_teams" in rule:
            rule["merge_whitelist_teams"] = kept
        return rule, None

    def _prot_get(self, m, body, params):
        if m[1] not in self.repos:
            return httpx.Response(404, json={"message": "no repo"})
        rule = self.protections[m[1]].get(m[2])
        if rule is None:
            return httpx.Response(404, json={"message": "no rule"})
        return httpx.Response(200, json=dict(rule))

    def _prot_create(self, m, body, params):
        repo = m[1]
        if repo not in self.repos:
            return httpx.Response(404, json={"message": "no repo"})
        name = body.get("rule_name")
        if not name:
            return httpx.Response(422, json={"message": "rule_name required"})
        if name in self.protections[repo]:
            return httpx.Response(403, json={"message": "rule exists"})
        rule, err = self._clean_protection(repo, body)
        if err is not None:
            return err
        self.protections[repo][name] = rule
        return httpx.Response(201, json=dict(rule))

    def _prot_edit(self, m, body, params):
        repo, name = m[1], m[2]
        if repo not in self.repos or name not in self.protections[repo]:
            return httpx.Response(404, json={"message": "no rule"})
        rule, err = self._clean_protection(repo, {**self.protections[repo][name], **body, "rule_name": name})
        if err is not None:
            return err
        self.protections[repo][name] = rule
        return httpx.Response(200, json=dict(rule))

    # hooks

    def _hooks_list(self, m, body, params):
        return httpx.Response(200, json=_page([dict(h) for h in self.hooks.values()], params))

    def _hook_create(self, m, body, params):
        if not body.get("type") or not isinstance(body.get("config"), dict):
            return httpx.Response(422, json={"message": "type and config required"})
        self._next_id += 1
        hook = {"id": self._next_id, "type": body["type"], "config": dict(body["config"]),
                "events": list(body.get("events") or []), "active": bool(body.get("active", False))}
        self.hooks[hook["id"]] = hook
        return httpx.Response(201, json=dict(hook))

    def _hook_edit(self, m, body, params):
        hook = self.hooks.get(int(m[1]))
        if hook is None:
            return httpx.Response(404, json={"message": "no hook"})
        if "config" in body:
            hook["config"] = {**hook["config"], **body["config"]}
        for k in ("events", "active"):
            if k in body:
                hook[k] = body[k]
        return httpx.Response(200, json=dict(hook))
