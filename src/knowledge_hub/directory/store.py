"""Directory snapshot store, membership cache and ``load_membership`` (plan 58, 3.6).

The serving process may import only ``store``, ``model`` and ``rules`` from this package.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from knowledge_hub.config import KhConfig
from knowledge_hub.directory.model import DirectoryUser, GroupFetch, Membership
from knowledge_hub.directory.rules import effective
from knowledge_hub.metrics import METRICS
from knowledge_hub.settings import Settings

log = logging.getLogger("knowledge_hub.directory")

SNAPSHOT_VERSION = 1


def _user_from_row(row: Any) -> DirectoryUser | None:
    if not isinstance(row, Mapping) or not isinstance(row.get("email"), str):
        return None
    username = row.get("username")
    return DirectoryUser(
        email=row["email"],
        enabled=row.get("enabled") is True,
        email_verified=row.get("email_verified") is True,
        username=username if isinstance(username, str) else "",
    )


def _users(rows: Any, group: str, role: str) -> tuple[DirectoryUser, ...]:
    if not isinstance(rows, list):
        log.warning("directory: snapshot group %s has no %s list; skipped", group, role)
        return ()
    out: list[DirectoryUser] = []
    for row in rows:
        user = _user_from_row(row)
        if user is None:
            log.warning("directory: snapshot group %s has a %s row of the wrong shape; skipped", group, role)
        else:
            out.append(user)
    return tuple(out)


def snapshot_to_fetched(data: Any) -> dict[str, GroupFetch]:
    groups = data.get("groups") if isinstance(data, Mapping) else None
    if not isinstance(groups, Mapping):
        return {}
    fetched: dict[str, GroupFetch] = {}
    for name, entry in groups.items():
        if not isinstance(name, str) or not isinstance(entry, Mapping):
            log.warning("directory: snapshot group entry of the wrong shape; skipped")
            continue
        fetched[name] = GroupFetch(
            members=_users(entry.get("members"), name, "member"),
            approvers=_users(entry.get("approvers"), name, "approver"),
        )
    return fetched


def _user_to_row(user: DirectoryUser) -> dict[str, Any]:
    return {
        "email": user.email,
        "enabled": user.enabled,
        "email_verified": user.email_verified,
        "username": user.username,
    }


def fetched_to_snapshot(provider: str, fetched: Mapping[str, GroupFetch]) -> dict[str, Any]:
    return {
        "version": SNAPSHOT_VERSION,
        "provider": provider,
        "groups": {
            name: {
                "members": [_user_to_row(u) for u in group.members],
                "approvers": [_user_to_row(u) for u in group.approvers],
            }
            for name, group in fetched.items()
        },
    }


async def load_snapshot(conn: psycopg.AsyncConnection) -> tuple[dict, datetime] | None:
    cur = await conn.execute("SELECT data, fetched_at FROM kh.directory_snapshot_get()")
    row = await cur.fetchone()
    return None if row is None else (row[0], row[1])


async def put_snapshot(conn: psycopg.AsyncConnection, data: dict) -> None:
    await conn.execute("SELECT kh.directory_snapshot_put(%s)", (Jsonb(data),))


def _usable(config: KhConfig, data: Any) -> bool:
    return (
        isinstance(data, Mapping)
        and data.get("version") == SNAPSHOT_VERSION
        and data.get("provider") == config.directory.provider
    )


class MembershipCache:
    def __init__(
        self,
        pool: Any,
        config: KhConfig,
        settings: Settings,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._pool = pool
        self._config = config
        self._settings = settings
        self._now = now
        self._static = Membership.from_config(config)
        self._directory: Membership | None = None
        self._fetched_at: float | None = None
        self._stale = False

    @property
    def _enabled(self) -> bool:
        return self._config.directory.provider != "none"

    def age_seconds(self) -> float | None:
        if not self._enabled or self._fetched_at is None:
            return None
        return self._now() - self._fetched_at

    async def refresh(self) -> None:
        if not self._enabled:
            return
        try:
            async with self._pool.connection() as conn:
                snap = await load_snapshot(conn)
        except Exception:
            log.exception("directory: snapshot read failed; keeping the previous membership")
            self._report_age()
            return
        if snap is None or not _usable(self._config, snap[0]):
            self._directory, self._fetched_at = None, None
        else:
            data, fetched_at = snap
            self._directory = effective(
                self._config, snapshot_to_fetched(data), self._config.directory.domains
            )
            self._fetched_at = fetched_at.timestamp()
        self._report_age()

    def _report_age(self) -> None:
        age = self.age_seconds()
        if age is not None:
            METRICS.directory_snapshot_age(age)

    def current(self) -> Membership:
        if not self._enabled:
            return self._static
        age = self.age_seconds()
        if age is not None:
            METRICS.directory_snapshot_age(age)
        stale = self._directory is None or age is None or age > self._config.directory.max_stale_seconds
        if stale != self._stale:
            self._stale = stale
            if stale:
                log.warning("directory: snapshot is missing or stale; serving static membership only")
            else:
                log.info("directory: snapshot is fresh again")
        return self._static if stale else self._directory  # type: ignore[return-value]


async def load_membership(config: KhConfig, settings: Settings) -> tuple[Membership, float | None]:
    static = Membership.from_config(config)
    if config.directory.provider == "none":
        return static, None
    try:
        async with await psycopg.AsyncConnection.connect(settings.dsn("migrator"), autocommit=True) as conn:
            snap = await load_snapshot(conn)
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedFunction):
        return static, None
    if snap is None:
        return static, None
    data, fetched_at = snap
    age = time.time() - fetched_at.timestamp()
    if not _usable(config, data) or age > config.directory.max_stale_seconds:
        return static, age
    membership = effective(config, snapshot_to_fetched(data), config.directory.domains)
    return membership, age
