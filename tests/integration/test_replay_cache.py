"""``PgReplayCache`` over ``kh.claim_seen``."""

from __future__ import annotations

import asyncio
import time

import pytest
from psycopg import errors

from knowledge_hub.auth.replay import PgReplayCache


async def test_first_add_is_new_second_is_replay(app_pool):
    cache = PgReplayCache(app_pool)
    exp = time.time() + 60
    assert await asyncio.to_thread(cache.add_if_new, "jti-1", exp) is True
    assert await asyncio.to_thread(cache.add_if_new, "jti-1", exp) is False
    assert await asyncio.to_thread(cache.add_if_new, "jti-2", exp) is True


async def test_refuses_to_block_the_event_loop(app_pool):
    with pytest.raises(RuntimeError):
        PgReplayCache(app_pool).add_if_new("jti-x", time.time() + 60)


async def test_app_pool_cannot_select_claim_jti(app_pool):
    async with app_pool.connection() as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            await conn.execute("SELECT * FROM kh.claim_jti")
