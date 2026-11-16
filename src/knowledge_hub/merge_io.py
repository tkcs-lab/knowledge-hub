"""Forgejo reads for the self-merge rule: teams, ``owners.yml`` and the tree diff."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from knowledge_hub.forgejo.client import ForgejoError
from knowledge_hub.governance.owners import OwnersError, OwnersFile, load_owners


class TeamLookupError(ForgejoError):
    """A team named in the configuration does not exist in Forgejo."""


class TeamDirectory:
    """Team ids and memberships, cached for one run."""

    def __init__(self, forgejo: Any) -> None:
        self._forgejo = forgejo
        self._ids: dict[str, int] = {}
        self._members: dict[tuple[str, str], bool] = {}

    async def _team_id(self, team: str) -> int:
        if team not in self._ids:
            found = await self._forgejo.team_id(team)
            if found is None:
                raise TeamLookupError(f"team {team} does not exist in Forgejo")
            self._ids[team] = found
        return self._ids[team]

    async def is_member(self, team: str, login: str) -> bool:
        key = (team, login.casefold())
        if key not in self._members:
            team_id = await self._team_id(team)
            self._members[key] = await self._forgejo.is_team_member(team_id, login)
        return self._members[key]

    async def teams_of(self, login: str, teams: Collection[str] | None) -> frozenset[str]:
        if not teams:
            return frozenset()
        return frozenset([t for t in sorted(teams) if await self.is_member(t, login)])


async def owners_at(forgejo: Any, repo: str, sha: str) -> OwnersFile | None:
    try:
        text = await forgejo.get_raw(repo, "owners.yml", sha)
    except ForgejoError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise
    try:
        return load_owners(text)
    except OwnersError:
        return None


async def tree_paths_changed(forgejo: Any, repo: str, base: str, head: str) -> tuple[str, ...]:
    def blobs(entries: list[dict[str, Any]]) -> dict[str, Any]:
        return {e["path"]: e.get("sha") for e in entries if e.get("type") != "tree"}

    before = blobs(await forgejo.list_tree(repo, base))
    after = blobs(await forgejo.list_tree(repo, head))
    changed = [
        p for p in before.keys() | after.keys() if (p in before) != (p in after) or before[p] != after[p]
    ]
    return tuple(sorted(changed))
