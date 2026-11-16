"""In-memory fake of ``ForgejoClient`` (no HTTP): reads and the promotion write side.

A repo is described by commits, oldest first. Each commit lists the file changes it makes relative to
its first parent (``None`` deletes a file); the tree at a commit is the first-parent chain applied in
order. PRs and reviews are given in Forgejo's JSON shapes (see ``indexer/trust.py``).

Writes (``create_branch``, ``put_file``, ``open_pull``) mutate ``branches``, ``files`` and ``opened``
and append every ref they touch to ``written_refs``, so a test can assert nothing reached ``main``.
The fake does not guard: the guard lives in the real client and is tested there.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from knowledge_hub.forgejo.client import ForgejoError


@dataclass
class FakeCommit:
    sha: str
    parents: list[str] = field(default_factory=list)
    files: dict[str, str | None] = field(default_factory=dict)
    date: str = "2026-11-01T10:00:00Z"


@dataclass
class FakeRepo:
    commits: list[FakeCommit]
    pulls: list[dict[str, Any]] = field(default_factory=list)
    reviews: dict[int, list[dict[str, Any]]] = field(default_factory=dict)


def pr(
    number: int,
    merge_sha: str,
    head_sha: str,
    author: str = "alice",
    *,
    merged_by: str | None = None,
    merged_at: str | None = None,
    author_id: int | None = None,
    merged_by_id: int | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "number": number,
        "html_url": f"https://forgejo.test/kh/repo/pulls/{number}",
        "merged": True,
        "merge_commit_sha": merge_sha,
        "user": {"login": author},
        "base": {"ref": "main"},
        "head": {"sha": head_sha},
    }
    if author_id is not None:
        out["user"]["id"] = author_id
    if merged_by is not None:
        out["merged_by"] = {"login": merged_by}
        if merged_by_id is not None:
            out["merged_by"]["id"] = merged_by_id
    if merged_at is not None:
        out["merged_at"] = merged_at
    return out


def approval(head_sha: str, user: str = "bob", at: str = "2026-11-02T10:00:00Z") -> dict[str, Any]:
    return {
        "state": "APPROVED",
        "official": True,
        "dismissed": False,
        "commit_id": head_sha,
        "submitted_at": at,
        "user": {"login": user},
    }


class ForgejoFake:
    def __init__(
        self,
        repos: dict[str, FakeRepo],
        read_only: bool = False,
        teams: dict[str, set[str]] | None = None,
    ) -> None:
        self.repos = repos
        self.teams = teams or {}
        # A read-only fake stands for the indexer client: a write is recorded in ``refused_writes``
        # and fails, so a test can assert no promotion write went through it.
        self.read_only = read_only
        self.refused_writes: list[str] = []
        self.calls: list[str] = []
        self.branches: dict[tuple[str, str], str] = {}
        self.files: dict[tuple[str, str, str], str] = {}
        self.labels: dict[str, set[str]] = {}
        self.opened: list[dict[str, Any]] = []
        self.written_refs: list[str] = []

    def _repo(self, repo: str) -> FakeRepo:
        try:
            return self.repos[repo]
        except KeyError:
            raise ForgejoError(f"unknown repo {repo}") from None

    def _commit(self, repo: str, sha: str) -> FakeCommit:
        for c in self._repo(repo).commits:
            if c.sha == sha:
                return c
        raise ForgejoError(f"unknown commit {sha}")

    def _snapshot(self, repo: str, sha: str) -> dict[str, str]:
        chain: list[FakeCommit] = []
        cur: str | None = sha
        while cur is not None:
            c = self._commit(repo, cur)
            chain.append(c)
            cur = c.parents[0] if c.parents else None
        tree: dict[str, str] = {}
        for c in reversed(chain):
            for path, text in c.files.items():
                if text is None:
                    tree.pop(path, None)
                else:
                    tree[path] = text
        return tree

    @staticmethod
    def _shape(c: FakeCommit) -> dict[str, Any]:
        return {
            "sha": c.sha,
            "parents": [{"sha": p} for p in c.parents],
            "commit": {"committer": {"date": c.date}},
        }

    async def head_sha(self, repo: str, branch: str) -> str:
        self.calls.append("head_sha")
        return self._repo(repo).commits[-1].sha

    async def list_tree(self, repo: str, sha: str) -> list[dict[str, Any]]:
        self.calls.append("list_tree")
        return [
            {
                "path": p,
                "type": "blob",
                "sha": "blob-" + hashlib.sha1(t.encode()).hexdigest(),
                "size": len(t),
            }
            for p, t in sorted(self._snapshot(repo, sha).items())
        ]

    async def get_raw(self, repo: str, path: str, sha: str) -> str:
        self.calls.append("get_raw")
        tree = self._snapshot(repo, sha)
        if path not in tree:
            raise ForgejoError(f"GET raw/{path} failed: HTTP 404")
        return tree[path]

    async def commits_first_parent(
        self, repo: str, branch: str, since_sha: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append("commits_first_parent")
        out: list[dict[str, Any]] = []
        cur: str | None = await self.head_sha(repo, branch)
        while cur is not None and cur != since_sha:
            c = self._commit(repo, cur)
            out.append(self._shape(c))
            cur = c.parents[0] if c.parents else None
        return out

    async def pull_for_commit(self, repo: str, sha: str) -> dict[str, Any] | None:
        self.calls.append("pull_for_commit")
        return next((p for p in self._repo(repo).pulls if p.get("merge_commit_sha") == sha), None)

    async def pull(self, repo: str, number: int) -> dict[str, Any]:
        self.calls.append("pull")
        for p in self._repo(repo).pulls:
            if p["number"] == number:
                return p
        raise ForgejoError(f"pull {number} not found")

    async def pull_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        self.calls.append("pull_reviews")
        return list(self._repo(repo).reviews.get(number, []))

    async def changed_files(self, repo: str, base: str, head: str) -> list[str]:
        self.calls.append("changed_files")
        a, b = self._snapshot(repo, base), self._snapshot(repo, head)
        return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))

    async def team_id(self, name: str) -> int | None:
        self.calls.append("team_id")
        names = list(self.teams)
        return names.index(name) + 1 if name in names else None

    async def is_team_member(self, team_id: int, login: str) -> bool:
        self.calls.append("is_team_member")
        names = list(self.teams)
        return 1 <= team_id <= len(names) and login in self.teams[names[team_id - 1]]

    # -- write side ----------------------------------------------------------------------------

    def _tree_at(self, repo: str, ref: str) -> dict[str, str]:
        if (repo, ref) in self.branches:
            tree = self._snapshot(repo, self.branches[(repo, ref)])
            tree.update({p: t for (r, b, p), t in self.files.items() if r == repo and b == ref})
            return tree
        if ref == "main":
            return self._snapshot(repo, self._repo(repo).commits[-1].sha)
        return self._snapshot(repo, ref)

    def _write(self, name: str) -> None:
        if self.read_only:
            self.refused_writes.append(name)
            raise ForgejoError(f"{name} refused: read-only client")

    async def path_exists(self, repo: str, path: str, ref: str) -> bool:
        self.calls.append("path_exists")
        return path in self._tree_at(repo, ref)

    async def create_branch(self, repo: str, name: str, from_sha: str) -> None:
        self._write("create_branch")
        self.calls.append("create_branch")
        self.written_refs.append(name)
        self._commit(repo, from_sha)
        if (repo, name) in self.branches:
            raise ForgejoError("POST branches failed: HTTP 409")
        self.branches[(repo, name)] = from_sha

    async def put_file(self, repo: str, branch: str, path: str, content: str, message: str) -> None:
        self._write("put_file")
        self.calls.append("put_file")
        self.written_refs.append(branch)
        if (repo, branch) not in self.branches:
            raise ForgejoError("PUT contents failed: HTTP 404")
        self.files[(repo, branch, path)] = content

    async def open_pull(
        self, repo: str, head: str, base: str, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        self._write("open_pull")
        self.calls.append("open_pull")
        self.written_refs.append(head)
        if (repo, head) not in self.branches:
            raise ForgejoError("POST pulls failed: HTTP 404")
        known = self.labels.get(repo)
        if known is not None and (missing := [n for n in labels if n not in known]):
            raise ForgejoError("label not found: " + ", ".join(missing))
        repo_obj = self._repo(repo)
        number = max([p["number"] for p in repo_obj.pulls] + [0]) + 1
        pull = {
            "number": number,
            "html_url": f"https://forgejo.test/kh/{repo}/pulls/{number}",
            "merged": False,
            "user": {"login": "kh-hub-bot"},
            "base": {"ref": base},
            "head": {"ref": head},
            "title": title,
            "body": body,
            "labels": [{"name": n} for n in labels],
        }
        repo_obj.pulls.append(pull)
        self.opened.append(pull)
        return pull
