"""Postgres store of the OAuth server: one ``kh.*`` definer function per method, called as ``kh_app``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class PendingRow:
    client_id: str
    params: dict[str, Any]
    nonce: str
    verifier: str


@dataclass(frozen=True)
class CodeRow:
    client_id: str
    email: str
    params: dict[str, Any]
    expires_at: datetime


@dataclass(frozen=True)
class TokenRow:
    family_id: UUID
    family_started_at: datetime
    client_id: str
    email: str
    scopes: list[str]
    resource: str
    expires_at: datetime
    used_at: datetime | None


class OAuthStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _one(self, sql: str, args: tuple[Any, ...]) -> tuple[Any, ...] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, args)
            return await cur.fetchone()

    async def _scalar(self, sql: str, args: tuple[Any, ...]) -> Any:
        row = await self._one(sql, args)
        return None if row is None else row[0]

    # clients
    async def client_get(self, client_id: str) -> dict[str, Any] | None:
        return await self._scalar("SELECT kh.oauth_client_get(%s)", (client_id,))

    async def client_put(self, client_id: str, info: dict[str, Any], max_clients: int) -> bool:
        return bool(
            await self._scalar(
                "SELECT kh.oauth_client_put(%s, %s, %s)", (client_id, Jsonb(info), max_clients)
            )
        )

    async def client_authorize(self, client_id: str) -> None:
        await self._scalar("SELECT kh.oauth_client_authorize(%s)", (client_id,))

    async def client_touch(self, client_id: str) -> None:
        await self._scalar("SELECT kh.oauth_client_touch(%s)", (client_id,))

    # pending rows (upstream state, consent ticket)
    async def pending_put(
        self,
        state_hash: bytes,
        kind: str,
        client_id: str,
        params: dict[str, Any],
        nonce: str,
        verifier: str,
        expires_at: datetime,
    ) -> None:
        await self._scalar(
            "SELECT kh.oauth_pending_put(%s, %s, %s, %s, %s, %s, %s)",
            (state_hash, kind, client_id, Jsonb(params), nonce, verifier, expires_at),
        )

    async def pending_take(self, state_hash: bytes, kind: str) -> PendingRow | None:
        row = await self._one("SELECT * FROM kh.oauth_pending_take(%s, %s)", (state_hash, kind))
        return None if row is None else PendingRow(*row)

    async def pending_peek(self, state_hash: bytes, kind: str) -> PendingRow | None:
        row = await self._one("SELECT * FROM kh.oauth_pending_peek(%s, %s)", (state_hash, kind))
        return None if row is None else PendingRow(*row)

    # authorization codes
    async def code_put(
        self, code_hash: bytes, client_id: str, email: str, params: dict[str, Any], expires_at: datetime
    ) -> None:
        await self._scalar(
            "SELECT kh.oauth_code_put(%s, %s, %s, %s, %s)",
            (code_hash, client_id, email, Jsonb(params), expires_at),
        )

    async def code_take(self, code_hash: bytes) -> CodeRow | None:
        row = await self._one("SELECT * FROM kh.oauth_code_take(%s)", (code_hash,))
        return None if row is None else CodeRow(*row)

    # tokens
    async def token_put(
        self,
        token_hash: bytes,
        kind: str,
        family_id: UUID | str,
        family_started: datetime,
        client_id: str,
        email: str,
        scopes: list[str],
        resource: str,
        expires_at: datetime,
    ) -> None:
        await self._scalar(
            "SELECT kh.oauth_token_put(%s, %s, %s::uuid, %s, %s, %s, %s::text[], %s, %s)",
            (
                token_hash,
                kind,
                str(family_id),
                family_started,
                client_id,
                email,
                list(scopes),
                resource,
                expires_at,
            ),
        )

    async def token_get(self, token_hash: bytes, kind: str) -> TokenRow | None:
        row = await self._one("SELECT * FROM kh.oauth_token_get(%s, %s)", (token_hash, kind))
        return None if row is None else TokenRow(*row)

    async def refresh_use(self, token_hash: bytes, grace_seconds: int) -> str:
        """Return ``ok``, ``reuse_grace``, ``reuse`` or ``gone``."""
        return str(await self._scalar("SELECT kh.oauth_refresh_use(%s, %s)", (token_hash, grace_seconds)))

    async def token_revoke(self, token_hash: bytes) -> None:
        await self._scalar("SELECT kh.oauth_token_revoke(%s)", (token_hash,))

    async def family_revoke(self, family_id: UUID | str) -> None:
        await self._scalar("SELECT kh.oauth_family_revoke(%s::uuid)", (str(family_id),))

    # consents
    async def consent_has(self, email: str, client_id: str) -> bool:
        return bool(await self._scalar("SELECT kh.oauth_consent_has(%s, %s)", (email, client_id)))

    async def consent_put(self, email: str, client_id: str) -> None:
        await self._scalar("SELECT kh.oauth_consent_put(%s, %s)", (email, client_id))
