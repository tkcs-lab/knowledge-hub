"""The forgejo step: teams, repos, protection, the org webhook, per-group Slack hooks (plan 3.7 step 3).

``plan`` only reads. ``apply`` repairs what it can and reports what it did with the same lines; whatever it
cannot repair stays a ``!`` line. Branch protection is read back after every write, because Forgejo silently
drops a merge-whitelist team that has no read access to the repo.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from knowledge_hub.config import KhConfig
from knowledge_hub.directory.model import Membership
from knowledge_hub.forgejo.admin import OWNERS_TEAM, ForgejoAdmin
from knowledge_hub.forgejo.reader import q
from knowledge_hub.setup.steps import Change

UNITS = ("repo.code", "repo.pulls")
REPO_SETTINGS: dict[str, Any] = {
    "private": True,
    "default_branch": "main",
    "has_actions": False,
    "default_delete_branch_after_merge": True,
}
HOOK_PATH = "/webhooks/forgejo"
HOOK_EVENTS = ["push"]
SLACK_EVENTS = ["pull_request"]
SLACK_URL_NAME = "KH_SETUP_SLACK_WEBHOOKS_FILE"
PUSH = frozenset({"write", "admin", "owner"})
ADMIN = frozenset({"admin", "owner"})


def team_fields(permission: str) -> dict[str, Any]:
    return {
        "permission": permission,
        "units_map": {unit: permission for unit in UNITS},
        "includes_all_repositories": False,
        "can_create_org_repo": False,
    }


def protection_body(group: str) -> dict[str, Any]:
    return {
        "rule_name": "main",
        "enable_push": False,
        "enable_push_whitelist": False,
        "enable_merge_whitelist": True,
        "merge_whitelist_teams": [f"{group}-approve"],
        "required_approvals": 0,
        "block_on_rejected_reviews": True,
        "apply_to_admins": True,
    }


def _units_differ(want: dict[str, str], have: Any) -> bool:
    have = have if isinstance(have, dict) else {}
    keys = set(want) | set(have)
    return any(want.get(k, "none") != have.get(k, "none") for k in keys)


def team_drift(want: dict[str, Any], have: dict[str, Any]) -> list[str]:
    out = []
    for field, value in want.items():
        if field == "units_map":
            if _units_differ(value, have.get(field)):
                out.append(field)
        elif have.get(field) != value:
            out.append(field)
    return out


def protection_drift(want: dict[str, Any], have: dict[str, Any]) -> list[str]:
    out = []
    for field, value in want.items():
        if field == "merge_whitelist_teams":
            if sorted(have.get(field) or []) != sorted(value):
                out.append(field)
        elif have.get(field) != value:
            out.append(field)
    return out


def _fold(logins: list[str]) -> dict[str, str]:
    return {login.casefold(): login for login in logins}


class ForgejoStep:
    name = "forgejo"

    def __init__(
        self,
        config: KhConfig,
        admin: ForgejoAdmin,
        *,
        webhook_secret: str | None,
        bot_token: str | None,
        indexer_token: str | None,
        tech_admin: ForgejoAdmin | None = None,
        slack_urls: dict[str, str] | None = None,
        membership_loader: Callable[[], Awaitable[tuple[Membership, float | None]]] | None = None,
    ) -> None:
        self._config = config
        self._loader = membership_loader
        self._membership = Membership.from_config(config)
        self._snapshot_age: float | None = None
        self._admin = admin
        self._tech_admin = tech_admin
        self._secret = webhook_secret
        self._slack_urls = slack_urls
        self._tokens = {config.bot_login: bot_token, config.indexer_login: indexer_token}

    async def plan(self) -> list[Change]:
        await self._load()
        return await self._sync(write=False, prune=False)

    async def apply(self, prune: bool) -> list[Change]:
        await self._load()
        return await self._sync(write=True, prune=prune)

    async def _load(self) -> None:
        if self._loader is None:
            self._membership, self._snapshot_age = Membership.from_config(self._config), None
        else:
            self._membership, self._snapshot_age = await self._loader()

    def _members(self, group: str) -> list[str]:
        return sorted(self._membership.members.get(group, ()))

    def _approvers(self, group: str) -> list[str]:
        return sorted(self._membership.approvers.get(group, ()))

    def directory_changes(self) -> list[Change]:
        directory = self._config.directory
        if directory.provider == "none":
            return []
        age = self._snapshot_age
        if age is None:
            return [Change("!", "directory: snapshot missing")]
        if age > directory.max_stale_seconds:
            limit = directory.max_stale_seconds
            return [Change("!", f"directory: snapshot is {int(age)}s old (limit {limit}s)")]
        return [Change("i", f"directory: {len(self._membership.people)} people from the directory snapshot")]

    # -- calls ForgejoAdmin has no public method for ----------------------------------------------

    async def _account(self, login: str) -> dict[str, Any] | None:
        return await self._admin._obj("GET", f"/users/{q(login)}", missing=True)  # noqa: SLF001

    async def _delete_team(self, team_id: int) -> None:
        await self._admin._request("DELETE", f"/teams/{team_id}", ok=(204,))  # noqa: SLF001

    # -- the walk --------------------------------------------------------------------------------

    async def _sync(self, *, write: bool, prune: bool) -> list[Change]:
        out: list[Change] = []
        cfg = self._config
        owners = sorted(str(u.get("login")) for u in await self._admin.org_owners())
        missing = await self._accounts(out, owners)
        await self._identity(out)

        teams = {str(t.get("name")): t for t in await self._admin.org_teams()}
        repos = {str(r.get("name")): r for r in await self._admin.repos()}
        for group in cfg.groups:
            await self._group(out, group, teams, repos, missing, write, prune)

        await self._tech(out, write, missing)
        await self._webhook(out, write)
        if self._tech_admin is not None:
            await self._webhook(out, write, admin=self._tech_admin, label_prefix="tech ")
        managed = {name for g in cfg.groups for name in (g, f"{g}-approve")} | {OWNERS_TEAM}
        for name, team in teams.items():
            if name in managed:
                continue
            if write and prune:
                await self._delete_team(int(team["id"]))
                out.append(Change("-", f"team {name}"))
            else:
                out.append(Change("?", f"unmanaged team {name}"))
        wanted = {cfg.repo_for_group(g) for g in cfg.groups}
        for name in repos:
            if name not in wanted:
                out.append(Change("?", f"unmanaged repo {name}"))
        if owners:
            out.append(Change("i", f"owners: {', '.join(owners)} can bypass protection"))
        out += self.directory_changes()
        return out

    async def sync_group(self, name: str, *, write: bool) -> list[Change]:
        """One group: its accounts, teams, repo, protection and Slack hook; no webhook or unmanaged lines."""
        out: list[Change] = []
        cfg = self._config
        gc = cfg.groups[name]
        await self._load()
        owners = sorted(str(u.get("login")) for u in await self._admin.org_owners())
        logins = [cfg.bot_login, cfg.indexer_login]
        if not gc.archived:
            logins += self._members(name) + self._approvers(name)
        missing = await self._check_accounts(out, logins, owners)
        teams = {str(t.get("name")): t for t in await self._admin.org_teams()}
        repos = {str(r.get("name")): r for r in await self._admin.repos()}
        await self._group(out, name, teams, repos, missing, write, False)
        return out

    async def remove_group(self, name: str, *, repo: str, write: bool) -> list[Change]:
        """Delete the group's teams; the repo and its Slack hook are kept."""
        out: list[Change] = []
        teams = {str(t.get("name")): t for t in await self._admin.org_teams()}
        for team_name in (name, f"{name}-approve"):
            team = teams.get(team_name)
            if team is None:
                continue
            out.append(Change("-", f"team {team_name}"))
            if write:
                await self._delete_team(int(team["id"]))
        if any(str(r.get("name")) == repo for r in await self._admin.repos()):
            out.append(Change("i", f"repo {repo}: kept"))
        return out

    async def missing_accounts(self, logins: list[str]) -> list[str]:
        return [login for login in logins if await self._account(login) is None]

    async def _group(
        self,
        out: list[Change],
        name: str,
        teams: dict[str, dict[str, Any]],
        repos: dict[str, dict[str, Any]],
        missing: set[str],
        write: bool,
        prune: bool,
    ) -> None:
        cfg = self._config
        gc = cfg.groups[name]
        repo = cfg.repo_for_group(name)
        if gc.archived:
            await self._archive(out, repos, name, repo, write)
            for team_name, keep in ((name, self._members(name)), (f"{name}-approve", self._approvers(name))):
                await self._trim(out, teams.get(team_name), team_name, keep, write)
            return
        write_team = await self._team(out, teams, name, "write", self._members(name), missing, write)
        approve_team = await self._team(
            out, teams, f"{name}-approve", "read", self._approvers(name), missing, write
        )
        repo_exists = await self._repo(out, repos, repo, write)
        for team_name, tid in ((name, write_team), (f"{name}-approve", approve_team)):
            await self._team_repo(out, team_name, tid, repo, repo_exists, write)
        for login, perm in ((cfg.bot_login, "write"), (cfg.indexer_login, "read")):
            await self._collaborator(out, repo, repo_exists, login, perm, login in missing, write)
        await self._protection(out, name, repo, repo_exists, write)
        await self._slack(out, name, repo, repo_exists, write, prune)

    async def _trim(self, out: list[Change], team: dict[str, Any] | None, name: str,
                    keep: list[str], write: bool) -> None:
        """Remove members the config dropped from an existing team; never add, never create."""
        if team is None:
            return
        tid = int(team["id"])
        wanted = _fold(keep)
        for login in [str(u.get("login")) for u in await self._admin.team_members(tid)]:
            if login.casefold() in wanted:
                continue
            out.append(Change("~", f"team {name}: members -{login}"))
            if write:
                await self._admin.remove_team_member(tid, login)

    async def _tech(self, out: list[Change], write: bool, missing: set[str]) -> None:
        """Tech repos are made by hand in the tech org: report them, give the indexer read, nothing else."""
        admin = self._tech_admin
        if admin is None:
            return
        cfg = self._config
        have = {str(r.get("name")) for r in await admin.repos()}
        for repo in cfg.tech_repo_names:
            await self._tech_repo(out, repo, have, write, missing, admin)
        for name in sorted(have - set(cfg.tech_repo_names)):
            out.append(Change("?", f"unmanaged tech repo {name}"))

    async def _tech_repo(
        self, out: list[Change], repo: str, have: set[str], write: bool, missing: set[str],
        admin: ForgejoAdmin,
    ) -> None:
        cfg = self._config
        if repo not in have:
            out.append(Change(
                "!", f"tech repo {repo}: not in org {cfg.tech_org}; create the clone or fork there"
            ))
            return
        await self._collaborator(
            out, repo, True, cfg.indexer_login, "read", cfg.indexer_login in missing, write,
            admin=admin,
        )
        logins = sorted(
            str(u.get("login")) for u in await admin.collaborators(repo)
            if str(u.get("login")).casefold() != cfg.indexer_login.casefold()
        )
        out.append(Change("i", f"tech repo {repo}: collaborators {', '.join(logins) or 'none'}"))

    async def sync_tech_repo(self, name: str, *, write: bool) -> list[Change]:
        """One tech repo: it exists in the tech org, the indexer reads it, the tech webhook is there."""
        out: list[Change] = []
        admin = self._tech_admin
        if admin is None:
            return [Change("!", f"tech repo {name}: tech_org is not set")]
        cfg = self._config
        owners = sorted(str(u.get("login")) for u in await self._admin.org_owners())
        missing = await self._check_accounts(out, [cfg.indexer_login], owners)
        have = {str(r.get("name")) for r in await admin.repos()}
        await self._tech_repo(out, name, have, write, missing, admin)
        if name in have:
            await self._webhook(out, write, admin=admin, label_prefix="tech ")
        return out

    async def _accounts(self, out: list[Change], owners: list[str]) -> set[str]:
        """Every login the config names must exist; the bots must be neither org owner nor site admin."""
        cfg = self._config
        logins = [cfg.bot_login, cfg.indexer_login]
        for group, gc in cfg.groups.items():
            if not gc.archived:
                logins += self._members(group) + self._approvers(group)
        return await self._check_accounts(out, logins, owners)

    async def _check_accounts(self, out: list[Change], logins: list[str], owners: list[str]) -> set[str]:
        cfg = self._config
        unique: dict[str, str] = {}
        for login in logins:
            unique.setdefault(login.casefold(), login)
        owner_set = {o.casefold() for o in owners}
        missing: set[str] = set()
        for folded, login in unique.items():
            account = await self._account(login)
            from_directory = login in self._membership.people and login not in cfg.people
            if account is None:
                missing.add(login)
                if from_directory:
                    out.append(Change(
                        "i", f"account {login}: no Forgejo account yet; not added to its teams"
                    ))
                else:
                    out.append(Change("!", f"account {login}: does not exist"))
                continue
            if from_directory and str(account.get("email") or "").lower() != self._membership.people[login]:
                missing.add(login)
                out.append(Change("!", f"account {login}: email differs from directory; not added"))
                continue
            if login in cfg.bots:
                if folded in owner_set:
                    out.append(Change("!", f"account {login}: is an org owner"))
                if account.get("is_admin"):
                    out.append(Change("!", f"account {login}: is a site admin"))
        return missing

    async def _identity(self, out: list[Change]) -> None:
        for login, token in self._tokens.items():
            if token is None:
                continue
            me = await self._admin.whoami(token)
            actual = str(me.get("login", ""))
            if actual.casefold() != login.casefold():
                out.append(Change("!", f"token {login}: belongs to {actual or 'nobody'}"))
            if me.get("is_admin"):
                out.append(Change("!", f"token {login}: is a site admin"))

    async def _team(
        self,
        out: list[Change],
        teams: dict[str, dict[str, Any]],
        name: str,
        permission: str,
        members: list[str],
        missing: set[str],
        write: bool,
    ) -> int | None:
        want = team_fields(permission)
        team = teams.get(name)
        wanted = {k: v for k, v in _fold(members).items() if v not in missing}
        if team is None:
            out.append(Change("+", f"team {name}: members {', '.join(wanted.values()) or '-'}"))
            if not write:
                return None
            team = await self._admin.create_team(name, permission, f"kh {name}", **{
                k: v for k, v in want.items() if k != "permission"
            })
            teams[name] = team
            tid = int(team["id"])
            for login in wanted.values():
                await self._admin.add_team_member(tid, login)
            return tid
        tid = int(team["id"])
        drift = team_drift(want, team)
        for field in drift:
            out.append(Change("~", f"team {name}: {field}"))
        if drift and write:
            await self._admin.edit_team(tid, **want)
        have = _fold([str(u.get("login")) for u in await self._admin.team_members(tid)])
        add = [v for k, v in wanted.items() if k not in have]
        remove = [v for k, v in have.items() if k not in wanted]
        if add or remove:
            detail = " ".join([f"+{x}" for x in add] + [f"-{x}" for x in remove])
            out.append(Change("~", f"team {name}: members {detail}"))
            if write:
                for login in add:
                    await self._admin.add_team_member(tid, login)
                for login in remove:
                    await self._admin.remove_team_member(tid, login)
        return tid

    async def _repo(self, out: list[Change], repos: dict[str, dict[str, Any]], repo: str,
                    write: bool) -> bool:
        have = repos.get(repo)
        if have is None:
            out.append(Change("+", f"repo {repo}"))
            if not write:
                return False
            created = await self._admin.create_repo(repo)
            fix = {k: v for k, v in REPO_SETTINGS.items() if created.get(k) != v}
            repos[repo] = await self._admin.edit_repo(repo, **fix) if fix else created
            return True
        drift = {k: v for k, v in REPO_SETTINGS.items() if have.get(k) != v}
        if have.get("archived"):
            drift["archived"] = False
        for field in drift:
            out.append(Change("~", f"repo {repo}: {field}"))
        if drift and write:
            repos[repo] = await self._admin.edit_repo(repo, **drift)
        return True

    async def _archive(self, out: list[Change], repos: dict[str, dict[str, Any]], group: str,
                       repo: str, write: bool) -> None:
        """An archived group keeps its teams; only the repo is archived and members the config dropped
        are trimmed."""
        have = repos.get(repo)
        if have is None:
            out.append(Change("i", f"group {group}: archived, repo {repo} not created"))
        elif not have.get("archived"):
            out.append(Change("~", f"repo {repo}: archived"))
            if write:
                repos[repo] = await self._admin.edit_repo(repo, archived=True)

    async def _team_repo(self, out: list[Change], name: str, tid: int | None, repo: str,
                         repo_exists: bool, write: bool) -> None:
        if tid is None or not repo_exists:
            out.append(Change("+", f"team repo {name}: {repo}"))
            return
        names = [str(r.get("name")) for r in await self._admin.team_repos(tid)]
        if repo not in names:
            out.append(Change("+", f"team repo {name}: {repo}"))
            if write:
                await self._admin.add_team_repo(tid, repo)
        for other in names:
            if other != repo:
                out.append(Change("!", f"team repo {name}: {other} is not the group repo; remove it"))

    async def _collaborator(self, out: list[Change], repo: str, repo_exists: bool, login: str,
                            perm: str, account_missing: bool, write: bool,
                            *, admin: ForgejoAdmin | None = None) -> None:
        admin = admin or self._admin
        label = f"collaborator {repo}/{login}"
        if account_missing:
            return
        if not repo_exists:
            out.append(Change("+", f"{label}: {perm}"))
            return
        listed = {str(u.get("login")).casefold() for u in await admin.collaborators(repo)}
        effective = await self._effective(repo, login, admin=admin)
        if login.casefold() not in listed:
            change = Change("+", f"{label}: {perm}")
        elif effective != perm:
            change = Change("~", f"{label}: permission {effective}, want {perm}")
        else:
            return
        if not write:
            out.append(change)
            return
        await admin.put_collaborator(repo, login, perm)
        effective = await self._effective(repo, login, admin=admin)
        out.append(change)
        if effective != perm:
            out.append(Change("!", f"{label}: permission {effective} after apply, want {perm}"))

    async def _effective(self, repo: str, login: str, *, admin: ForgejoAdmin | None = None) -> str:
        data = await (admin or self._admin).user_repo_permission(repo, login)
        return str(data.get("permission", "none"))

    async def _protection(self, out: list[Change], group: str, repo: str, repo_exists: bool,
                          write: bool) -> None:
        label = f"protection {repo}/main"
        want = protection_body(group)
        have = await self._admin.protection(repo) if repo_exists else None
        if have is None:
            out.append(Change("+", label))
        else:
            drift = protection_drift(want, have)
            for field in drift:
                out.append(Change("~", f"{label}: {field}"))
            if not drift:
                return
        if not write:
            return
        await self._admin.put_protection(repo, want)
        back = await self._admin.protection(repo)
        if back is None:
            out.append(Change("!", f"{label}: missing after write"))
            return
        for field in protection_drift(want, back):
            out.append(Change("!", f"{label}: {field} not kept after write"))

    async def _slack(
        self, out: list[Change], group: str, repo: str, repo_exists: bool, write: bool, prune: bool
    ) -> None:
        label = f"slack hook {repo}"
        channel = self._config.groups[group].slack_channel
        hooks = (
            [h for h in await self._admin.repo_hooks(repo) if h.get("type") == "slack"]
            if repo_exists
            else []
        )
        if channel is None:
            for hook in hooks:
                if write and prune:
                    await self._admin.delete_repo_hook(repo, int(hook["id"]))
                    out.append(Change("-", label))
                else:
                    out.append(Change("?", f"unmanaged {label}"))
            return
        if len(hooks) > 1:
            out.append(Change("!", f"{label}: {len(hooks)} Slack hooks; delete the extra ones in Forgejo"))
            return
        url = (self._slack_urls or {}).get(group)
        if write and url is None:
            out.append(Change("!", f"{label}: no URL for group {group} in {SLACK_URL_NAME}"))
            return
        hook = hooks[0] if hooks else None
        drift: list[str] = []
        if hook is not None:
            config = hook.get("config") or {}
            if "pull_request" not in (hook.get("events") or []):
                drift.append("events")
            if config.get("content_type") != "json":
                drift.append("content_type")
            if "channel" in config and config["channel"] != channel:
                drift.append("channel")
            if hook.get("active") is not True:
                drift.append("active")
        if not write:
            if hook is None:
                out.append(Change("+", f"{label}: {channel}"))
                return
            out.extend(Change("~", f"{label}: {field}") for field in drift)
            out.append(Change("i", f"{label}: url not verifiable"))
            return
        body = {
            "config": {"url": url, "content_type": "json", "channel": channel},
            "events": SLACK_EVENTS,
            "active": True,
        }
        if hook is None:
            await self._admin.create_repo_hook(repo, {"type": "slack", **body})
            out.append(Change("+", f"{label}: {channel}"))
            return
        await self._admin.edit_repo_hook(repo, int(hook["id"]), body)
        out.extend(Change("~", f"{label}: {field}") for field in drift)
        out.append(Change("i", f"{label}: url rewritten"))

    async def _webhook(self, out: list[Change], write: bool, *, admin: ForgejoAdmin | None = None,
                       label_prefix: str = "") -> None:
        admin = admin or self._admin
        hub = self._config.hub_url
        if hub is None:
            out.append(Change("!", f"{label_prefix}hub_url: not set"))
            return
        url = hub.rstrip("/") + HOOK_PATH
        label = f"{label_prefix}webhook {url}"
        hook = next(
            (h for h in await admin.org_hooks()
             if isinstance(h.get("config"), dict) and h["config"].get("url") == url),
            None,
        )
        drift: list[str] = []
        if hook is not None:
            config = hook["config"]
            if hook.get("type") != "forgejo":
                drift.append("type")
            if sorted(hook.get("events") or []) != HOOK_EVENTS:
                drift.append("events")
            if config.get("content_type") != "json":
                drift.append("content_type")
            if hook.get("active") is not True:
                drift.append("active")
        if not write:
            if hook is None:
                out.append(Change("+", label))
            for field in drift:
                out.append(Change("~", f"{label}: {field}"))
            out.append(Change("i", f"{label_prefix}webhook secret not verifiable"))
            return
        if not self._secret:
            out.append(Change("!", f"{label}: no secret (KH_WEBHOOK_HMAC_FILE not set)"))
            return
        body = {
            "config": {"url": url, "content_type": "json", "secret": self._secret},
            "events": HOOK_EVENTS,
            "active": True,
        }
        if hook is None:
            await admin.create_org_hook({"type": "forgejo", **body})
            out.append(Change("+", label))
            return
        if "type" in drift:
            # Forgejo cannot change the type of a hook; refuse rather than leave a second hook.
            out.append(Change("!", f"{label}: type is {hook.get('type')}; delete the hook in Forgejo"))
            return
        await admin.edit_org_hook(int(hook["id"]), body)
        for field in drift:
            out.append(Change("~", f"{label}: {field}"))
        out.append(Change("i", f"{label}: secret rewritten"))
