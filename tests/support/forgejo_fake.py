"""An in-memory Forgejo behind ``httpx.MockTransport``, for the reader, the bot and later phases.

Every commit holds a full snapshot of the files, so a branch is just a pointer to a commit. Blob shas are
real git blob hashes of the content; commit shas are fresh hashes per commit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

import httpx

PAGE_MAX = 1000


def blob_sha(text: str) -> str:
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 (git object id)


@dataclass
class _Commit:
    sha: str
    parents: list[str]
    files: dict[str, str]


@dataclass
class _Repo:
    name: str
    commits: dict[str, _Commit] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)
    pulls: list[dict] = field(default_factory=list)
    default_branch: str = "main"


class FakeForgejo:
    """Use ``fake.transport`` as the ``transport=`` of a client; ``fake.requests`` logs ``(method, path)``."""

    def __init__(self, org: str = "kh", token: str = "tok", login: str = "kh-bot") -> None:
        self.org = org
        self.token = token
        self.login = login  # the account that opens pull requests and pushes bot branches
        self.repos: dict[str, _Repo] = {}
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self._counter = 0
        self._failures: list[tuple[str, int]] = []
        self.transport = httpx.MockTransport(self._handle)

    # -- test setup ------------------------------------------------------------------------------

    def add_repo(self, name: str, branch: str = "main") -> None:
        repo = _Repo(name, default_branch=branch)
        self.repos[name] = repo
        sha = self._new_commit(repo, [], {})
        repo.branches[branch] = sha

    def commit(self, repo: str, changes: dict[str, str | None], branch: str | None = None) -> str:
        """Commit ``changes`` (a text, or None to delete) onto ``branch`` (default: the repo's default
        branch); returns the new head sha."""
        r = self.repos[repo]
        branch = branch or r.default_branch
        parent = r.branches[branch]
        files = dict(r.commits[parent].files)
        for path, text in changes.items():
            if text is None:
                files.pop(path, None)
            else:
                files[path] = text
        sha = self._new_commit(r, [parent], files)
        r.branches[branch] = sha
        return sha

    def rewrite(self, repo: str, files: dict[str, str], branch: str = "main") -> str:
        """Point ``branch`` at a new root commit: history is replaced, the old head is no ancestor."""
        r = self.repos[repo]
        sha = self._new_commit(r, [], dict(files))
        r.branches[branch] = sha
        return sha

    def head(self, repo: str, branch: str = "main") -> str:
        return self.repos[repo].branches[branch]

    def files(self, repo: str, ref: str = "main") -> dict[str, str]:
        r = self.repos[repo]
        return dict(r.commits[r.branches.get(ref, ref)].files)

    def fail_next(self, path_part: str, status: int) -> None:
        """The next request whose path contains ``path_part`` answers ``status``."""
        self._failures.append((path_part, status))

    def _new_commit(self, repo: _Repo, parents: list[str], files: dict[str, str]) -> str:
        self._counter += 1
        seed = f"{repo.name}:{self._counter}:{','.join(parents)}"
        sha = hashlib.sha1(seed.encode()).hexdigest()  # noqa: S324
        repo.commits[sha] = _Commit(sha, parents, files)
        return sha

    def _reachable(self, repo: _Repo, sha: str) -> set[str]:
        seen: set[str] = set()
        todo = [sha]
        while todo:
            cur = todo.pop()
            if cur in seen or cur not in repo.commits:
                continue
            seen.add(cur)
            todo.extend(repo.commits[cur].parents)
        return seen

    def _resolve(self, repo: _Repo, ref: str) -> str | None:
        if ref in repo.branches:
            return repo.branches[ref]
        return ref if ref in repo.commits else None

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
        if request.headers.get("Authorization") != f"token {self.token}":
            return httpx.Response(401, json={"message": "unauthorized"})
        m = re.fullmatch(r"/api/v1/repos/([^/]+)/([^/]+)(?:/(.*))?", path)
        if not m or m[1] != self.org or m[2] not in self.repos:
            return httpx.Response(404, json={"message": "not found"})
        repo, rest = self.repos[m[2]], m[3] or ""
        params = request.url.params
        return self._route(method, repo, rest, params, body)

    def _route(self, method: str, repo: _Repo, rest: str, params, body: dict) -> httpx.Response:  # noqa: C901
        def js(status: int, data) -> httpx.Response:
            return httpx.Response(status, json=data)

        if method == "GET" and rest == "":
            return js(200, {"name": repo.name, "default_branch": repo.default_branch})

        if method == "GET" and (m := re.fullmatch(r"branches/(.+)", rest)):
            sha = repo.branches.get(m[1])
            return js(200, {"name": m[1], "commit": {"id": sha}}) if sha else js(404, {})

        if method == "POST" and rest == "branches":
            name, old = body.get("new_branch_name"), body.get("old_ref_name", "")
            src = self._resolve(repo, old)
            if not name or src is None:
                return js(404, {})
            if name in repo.branches:
                return js(409, {})
            repo.branches[name] = src
            return js(201, {"name": name, "commit": {"id": src}})

        if method == "GET" and (m := re.fullmatch(r"git/trees/(.+)", rest)):
            sha = self._resolve(repo, m[1])
            if sha is None:
                return js(404, {})
            entries = self._tree(repo.commits[sha].files)
            per = min(int(params.get("per_page", 1000)), PAGE_MAX)
            page = max(int(params.get("page", 1)), 1)
            return js(
                200,
                {
                    "sha": sha,
                    "tree": entries[(page - 1) * per : page * per],
                    "total_count": len(entries),
                    "page": page,
                    "truncated": False,
                },
            )

        if method == "GET" and (m := re.fullmatch(r"raw/(.+)", rest)):
            sha = self._resolve(repo, params.get("ref", "main"))
            files = repo.commits[sha].files if sha else {}
            if m[1] not in files:
                return js(404, {})
            return httpx.Response(200, content=files[m[1]].encode("utf-8"))

        if re.fullmatch(r"contents/(.+)", rest):
            return self._contents(method, repo, rest[len("contents/") :], params, body)

        if method == "GET" and (m := re.fullmatch(r"compare/(.+)\.\.\.(.+)", rest)):
            a, b = self._resolve(repo, m[1]), self._resolve(repo, m[2])
            if a is None or b is None:
                return js(404, {})
            only_b = self._reachable(repo, b) - self._reachable(repo, a)
            return js(200, {"total_commits": len(only_b), "commits": [{"sha": s} for s in sorted(only_b)]})

        if rest == "pulls" and method == "POST":
            return self._open_pull(repo, body)

        if rest == "pulls" and method == "GET":
            state = params.get("state", "open")
            items = [p for p in repo.pulls if state == "all" or p["state"] == state]
            limit = int(params.get("limit", 50))
            page = max(int(params.get("page", 1)), 1)
            return js(200, items[(page - 1) * limit : page * limit])

        return js(404, {"message": "no such endpoint"})

    @staticmethod
    def _tree(files: dict[str, str]) -> list[dict]:
        dirs: set[str] = set()
        for p in files:
            parts = p.split("/")
            dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        entries = [{"path": d, "type": "tree", "sha": hashlib.sha1(d.encode()).hexdigest(), "size": 0}  # noqa: S324
                   for d in dirs]
        entries += [
            {"path": p, "type": "blob", "sha": blob_sha(t), "size": len(t.encode("utf-8"))}
            for p, t in files.items()
        ]
        return sorted(entries, key=lambda e: e["path"])

    def _contents(self, method: str, repo: _Repo, path: str, params, body: dict) -> httpx.Response:
        def js(status: int, data) -> httpx.Response:
            return httpx.Response(status, json=data)

        if method == "GET":
            sha = self._resolve(repo, params.get("ref", "main"))
            files = repo.commits[sha].files if sha else {}
            if path not in files:
                return js(404, {})
            return js(200, {"path": path, "type": "file", "sha": blob_sha(files[path])})

        if method not in ("POST", "PUT"):
            return js(405, {})
        branch = body.get("branch")
        head = repo.branches.get(branch)
        if head is None:
            return js(404, {})
        files = repo.commits[head].files
        if method == "POST" and path in files:
            return js(422, {"message": "repository file already exists"})
        if method == "PUT" and (path not in files or body.get("sha") != blob_sha(files[path])):
            return js(409 if path in files else 404, {"message": "sha does not match"})
        try:
            text = base64.b64decode(body["content"], validate=True).decode("utf-8")
        except (KeyError, ValueError):
            return js(422, {"message": "bad content"})
        sha = self.commit(repo.name, {path: text}, branch=branch)
        return js(
            201 if method == "POST" else 200,
            {
                "content": {"path": path, "sha": blob_sha(text)},
                "commit": {"sha": sha, "parents": [{"sha": p} for p in repo.commits[sha].parents]},
            },
        )

    def _open_pull(self, repo: _Repo, body: dict) -> httpx.Response:
        head, base = body.get("head"), body.get("base")
        if head not in repo.branches or base not in repo.branches:
            return httpx.Response(404, json={})
        if any(p["head"]["ref"] == head and p["state"] == "open" for p in repo.pulls):
            return httpx.Response(409, json={"message": "pull request already exists"})
        number = len(repo.pulls) + 1
        pull = {
            "number": number,
            "state": "open",
            "title": body.get("title", ""),
            "body": body.get("body", ""),
            "html_url": f"https://forgejo.test/{self.org}/{repo.name}/pulls/{number}",
            "head": {"ref": head, "sha": repo.branches[head]},
            "base": {"ref": base},
            "user": {"login": self.login},
        }
        repo.pulls.append(pull)
        return httpx.Response(201, json=pull)


def md(doc_id: str, title: str = "T", *, labels: list[str] | None = None, review_by: str = "2099-01-01",
       body: str = "Body text.\n", supersedes: list[str] | None = None) -> str:
    """A Markdown file with the required frontmatter (``labels`` omitted when None)."""
    lab = "" if labels is None else f"labels: [{', '.join(labels)}]\n"
    sup = "" if supersedes is None else f"supersedes: [{', '.join(supersedes)}]\n"
    return f"---\nid: {doc_id}\ntitle: {title}\nreview_by: {review_by}\n{lab}{sup}---\n{body}"
