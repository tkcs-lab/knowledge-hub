"""Usage events: who searched or opened what, written after the tool transaction (plan 3.4).

Best effort: a failure here is counted and logged, never shown to the caller. This module never
imports ``knowledge_hub.mcp.errors``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from knowledge_hub.auth.claims import Caller
from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.metrics import METRICS

if TYPE_CHECKING:
    from knowledge_hub.mcp.server import Deps

log = logging.getLogger("knowledge_hub.usage")

WRITE_TIMEOUT = 0.25  # seconds to get a pooled connection

_INSERT = (
    "INSERT INTO kh.usage_events"
    " (id, kind, who, audiences, query, min_tier, t0_fts_hits, t3_fts_hits, owner_keys)"
    " VALUES (%s, %s, %s, kh.caller_audiences(), %s, %s, %s, %s, %s)"
)


@dataclass(frozen=True)
class UsageEvent:
    kind: str
    owner_keys: tuple[str, ...]
    query: str | None = None
    min_tier: str | None = None
    t0_fts_hits: int = 0
    t3_fts_hits: int = 0

    @classmethod
    def searched(
        cls,
        query: str,
        min_tier: str,
        t0_fts_hits: int,
        t3_fts_hits: int,
        owner_keys: Sequence[str],
    ) -> UsageEvent:
        return cls(
            kind="search",
            owner_keys=tuple(owner_keys),
            query=query.replace("\x00", "")[:500],
            min_tier=min_tier,
            t0_fts_hits=t0_fts_hits,
            t3_fts_hits=t3_fts_hits,
        )

    @classmethod
    def opened(cls, owner_key: str) -> UsageEvent:
        return cls(kind="open", owner_keys=(owner_key,))


def searcher_hash(key: bytes, sub: str) -> str:
    sub_key = hmac.new(key, b"kh-usage-v1", hashlib.sha256).digest()
    return hmac.new(sub_key, sub.encode(), hashlib.sha256).hexdigest()[:32]


async def write_events(deps: Deps, caller: Caller, events: Sequence[UsageEvent]) -> None:
    if (
        not events
        or not deps.settings.usage_log
        or caller.principal_type != "claude_client"
        or not caller.audiences
        or deps.settings.cursor_key_file is None
    ):
        return
    try:
        key = Path(deps.settings.cursor_key_file).read_bytes().strip()
        who = searcher_hash(key, caller.sub)
        rows = [
            (uuid.uuid4(), e.kind, who, e.query, e.min_tier, e.t0_fts_hits, e.t3_fts_hits,
             list(e.owner_keys))
            for e in events
        ]  # fmt: skip
        async with caller_tx(deps.app_pool, caller, timeout=WRITE_TIMEOUT) as conn:
            async with conn.cursor() as cur:
                await cur.executemany(_INSERT, rows)
    except Exception:
        log.warning("usage events dropped", exc_info=True)
        METRICS.usage_dropped(len(events))
