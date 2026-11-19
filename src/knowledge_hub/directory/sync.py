"""The directory sync process (plan 58, 3.6): fetch, guard, store, log.

Runs only in ``kh directory sync``; the serving app must not import this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from knowledge_hub.config import KhConfig
from knowledge_hub.config_watch import EXIT_RESTART
from knowledge_hub.directory import store
from knowledge_hub.directory.model import GroupFetch
from knowledge_hub.directory.provider import DirectoryProvider, ProviderError, refs_from_config
from knowledge_hub.directory.rules import effective
from knowledge_hub.settings import Settings

log = logging.getLogger("knowledge_hub.directory")

REFUSED_MARKER = "directory-refused"


class ChangeRefused(Exception):
    """The new snapshot differs too much from the stored one; rerun with ``--accept-change``."""


@dataclass(frozen=True)
class SyncResult:
    users: int
    groups: int
    skips: int


def count_users(fetched: Mapping[str, GroupFetch]) -> int:
    """Distinct lower-case addresses over all groups and roles."""
    emails = {
        u.email.strip().lower() for group in fetched.values() for u in (*group.members, *group.approvers)
    }
    return len(emails)


def _guard(stored: int, new: int, percent: int) -> None:
    shrunk = new * 100 < stored * (100 - percent)
    grew = stored >= 10 and new > 2 * stored
    if shrunk or grew:
        raise ChangeRefused(
            f"directory: refusing snapshot: {stored} users changed to {new}; rerun with --accept-change"
        )


async def sync_once(
    config: KhConfig,
    settings: Settings,
    provider: DirectoryProvider,
    pool: Any,
    *,
    accept_change: bool = False,
) -> SyncResult:
    refs = refs_from_config(config, provider.name)
    fetched = await provider.fetch(refs)
    missing = sorted(set(refs) - set(fetched))
    if missing:
        raise ProviderError(f"fetch did not return group(s) {', '.join(missing)}")
    fetched = {name: fetched[name] for name in refs}
    data = store.fetched_to_snapshot(provider.name, fetched)
    domains = config.directory.domains

    async with pool.connection() as conn:
        stored = await store.load_snapshot(conn)
    previous = (
        effective(config, store.snapshot_to_fetched(stored[0]), domains)
        if stored
        else effective(config, None, domains)
    )
    if stored is not None and not accept_change:
        _guard(
            count_users(store.snapshot_to_fetched(stored[0])),
            count_users(fetched),
            config.directory.shrink_guard_percent,
        )

    async with pool.connection() as conn, conn.transaction():
        await store.put_snapshot(conn, data)

    membership = effective(config, fetched, domains)
    users = count_users(fetched)
    log.info(
        "directory: %s %d users in %d groups, %d skipped",
        provider.name,
        users,
        len(fetched),
        len(membership.skips),
    )
    for skip in membership.skips:
        log.info("directory: skip %s %s %s: %s", skip.group, skip.role, skip.who, skip.reason)
    for group in sorted(membership.approvers):
        added = membership.approvers[group] - previous.approvers.get(group, frozenset())
        if added:
            log.info("directory: approvers added in %s: %s", group, ", ".join(sorted(added)))
    return SyncResult(users=users, groups=len(fetched), skips=len(membership.skips))


async def run_every(
    config: KhConfig,
    settings: Settings,
    provider: DirectoryProvider,
    pool: Any,
    *,
    interval: float,
    accept_change: bool,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    watcher: Any = None,
) -> int:
    while True:
        try:
            await sync_once(config, settings, provider, pool, accept_change=accept_change)
        except ChangeRefused as exc:
            log.error("%s: %s", REFUSED_MARKER, exc)
        except ProviderError as exc:
            log.error("directory: %s: %s", provider.name, exc)
        except Exception:
            log.exception("directory: sync run failed; keeping the previous snapshot")
        await sleep(interval)
        if watcher is not None and watcher.poll():
            log.info(
                "directory: config changed on disk; exiting with code %d so the container restarts",
                EXIT_RESTART,
            )
            return EXIT_RESTART
