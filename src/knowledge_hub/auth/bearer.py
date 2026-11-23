"""The real bearer resolver: hub keys (``khk_``) and OAuth access tokens (``kha_``) (plan 3.10)."""

from __future__ import annotations

from typing import Any

from knowledge_hub.auth.claims import Caller
from knowledge_hub.auth.keys import verify_key

__all__ = ["KEY_ONLY_TOOLS", "READ_TOOLS", "WRITE_TOOLS", "BearerResolver"]

READ_TOOLS = frozenset({"search", "get_doc", "list_stale", "list_labels", "usage", "list_datasets", "query"})
WRITE_TOOLS = frozenset({"mark_record_outdated", "propose_doc", "save_query"})
KEY_ONLY_TOOLS = frozenset({"ingest_record", "upsert_rows", "delete_rows", "finish_sync"})

UNKNOWN = "bearer_unknown"
RESOURCE = "bearer_resource"


class BearerResolver:
    def __init__(
        self, pool: Any, config: Any, provider: Any, membership: Any, resource_url: str | None
    ) -> None:
        self._pool = pool
        self._config = config
        self._provider = provider
        self._membership = membership
        self._resource_url = resource_url

    async def resolve(self, token: str) -> tuple[Caller | None, str]:
        if token.startswith("khk_"):
            caller = await verify_key(self._pool, self._config, token)
            return (caller, "") if caller is not None else (None, UNKNOWN)
        if token.startswith("kha_") and self._provider is not None:
            return await self._resolve_oauth(token)
        return None, UNKNOWN

    async def _resolve_oauth(self, token: str) -> tuple[Caller | None, str]:
        access = await self._provider.load_access_token(token)
        if access is None:
            return None, UNKNOWN
        if self._resource_url is None or access.resource != self._resource_url:
            return None, RESOURCE
        email = (access.subject or "").lower()
        if not email:
            return None, UNKNOWN
        scopes = set(access.scopes or [])
        tools: set[str] = set()
        if "kh:read" in scopes:
            tools |= READ_TOOLS
        if "kh:write" in scopes:
            tools |= WRITE_TOOLS
        audiences = self._membership.current().audiences_for_email(email, self._config.is_active)
        return (
            Caller(
                sub=f"u:{email}",
                principal_type="claude_client",
                email=email,
                audiences=frozenset(audiences),
                token_id=None,
                tools=frozenset(tools),
            ),
            "",
        )
