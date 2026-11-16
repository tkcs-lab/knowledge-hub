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
from collections.abc import Callable
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
        # Commits made on branches by ``put_file``; never part of ``repos[repo].commits`` (the main chain).
        self.branch_commits: dict[str, list[FakeCommit]] = {}
        self.messages: list[tuple[str, str, str, str]] = []
        self.statuses: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.unknown_users: set[str] = set()
        self.fail_after_commit = False
        self.on_put_file: Callable[[], None] | None = None
        self._counter = 0

    def _next(self) -> int:
        self._counter += 1
        return self._counter

    def _repo(self, repo: str) -> FakeRepo:
        try:
            return self.repos[repo]
        except KeyError:
            raise ForgejoError(f"unknown repo {repo}") from None

    def _commit(self, repo: str, sha: str) -> FakeCommit:
        for c in [*self._repo(repo).commits, *self.branch_commits.get(repo, [])]:
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
        if (repo, branch) in self.branches:
            return self.branches[(repo, branch)]
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

    async def put_file(
        self, repo: str, branch: str, path: str, content: str, message: str
    ) -> dict[str, Any]:
        self._write("put_file")
        self.calls.append("put_file")
        self.written_refs.append(branch)
        if (repo, branch) not in self.branches:
            raise ForgejoError("PUT contents failed: HTTP 404")
        if self.on_put_file is not None:
            self.on_put_file()
        parent = self.branches[(repo, branch)]
        commit = FakeCommit(f"b{self._next()}", parents=[parent], files={path: content})
        self.branch_commits.setdefault(repo, []).append(commit)
        self.branches[(repo, branch)] = commit.sha
        self.files[(repo, branch, path)] = content
        self.messages.append((repo, branch, path, message))
        return {"sha": commit.sha, "parents": [parent]}

    def _resolve(self, repo: str, ref: str) -> str:
        if (repo, ref) in self.branches:
            return self.branches[(repo, ref)]
        if ref == "main":
            return self._repo(repo).commits[-1].sha
        return ref

    async def compare_files(self, repo: str, base: str, head: str) -> list[dict[str, Any]]:
        self.calls.append("compare_files")
        a, b = self._tree_at(repo, base), self._tree_at(repo, head)
        out = []
        for p in sorted(set(a) | set(b)):
            if a.get(p) == b.get(p):
                continue
            status = "added" if p not in a else "removed" if p not in b else "modified"
            out.append({"filename": p, "status": status})
        return out

    async def blob_sha(self, repo: str, path: str, ref: str) -> str | None:
        self.calls.append("blob_sha")
        text = self._tree_at(repo, ref).get(path)
        if text is None:
            return None
        data = text.encode("utf-8")
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324

    async def last_commit(self, repo: str, path: str, ref: str) -> str | None:
        self.calls.append("last_commit")
        cur: str | None = self._resolve(repo, ref)
        while cur is not None:
            c = self._commit(repo, cur)
            if path in c.files:
                return c.sha
            cur = c.parents[0] if c.parents else None
        return None

    async def put_note(
        self, repo: str, path: str, content: str, message: str, prev_blob: str | None
    ) -> str:
        self._write("put_note")
        self.calls.append("put_note")
        current = await self.blob_sha(repo, path, "main")
        if prev_blob is None and current is not None:
            raise ForgejoError("POST contents failed: HTTP 422")
        if prev_blob is not None and prev_blob != current:
            raise ForgejoError("PUT contents failed: HTTP 409")
        repo_obj = self._repo(repo)
        commit = FakeCommit(
            f"n{self._next()}", parents=[repo_obj.commits[-1].sha], files={path: content}
        )
        repo_obj.commits.append(commit)
        self.messages.append((repo, "main", path, message))
        self.written_refs.append("main")
        if self.fail_after_commit:
            raise ForgejoError("PUT contents failed: timeout")
        return commit.sha

    async def commit_statuses(self, repo: str, sha: str) -> list[dict[str, Any]]:
        self.calls.append("commit_statuses")
        return list(self.statuses.get((repo, sha), []))

    async def post_requester(self, repo: str, sha: str, login: str, proposal_id: str) -> None:
        self._write("post_requester")
        self.calls.append("post_requester")
        self.statuses.setdefault((repo, sha), []).append(
            {
                "context": "kh/requested-by",
                "state": "success",
                "description": f"{login} {proposal_id}",
                "creator": "kh-hub-bot",
            }
        )

    async def pull_for_head(self, repo: str, branch: str) -> dict[str, Any] | None:
        self.calls.append("pull_for_head")
        return next(
            (p for p in self._repo(repo).pulls if (p.get("head") or {}).get("ref") == branch), None
        )

    async def org_member(self, login: str) -> bool:
        self.calls.append("org_member")
        return login not in self.unknown_users

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
            "head": {"ref": head, "sha": self.branches[(repo, head)]},
            "state": "open",
            "title": title,
            "body": body,
            "labels": [{"name": n} for n in labels],
        }
        repo_obj.pulls.append(pull)
        self.opened.append(pull)
        return pull
