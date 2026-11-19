"""``HubAuthProvider``: the MCP SDK's authorization-server provider backed by the Postgres store.

It implements the SDK protocol only. The upstream callback, the consent page and code issuing live in
``oauth/flow.py`` (plan 58, 3.9).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from knowledge_hub.config import KhConfig
from knowledge_hub.directory.model import MembershipView
from knowledge_hub.oauth.allowlist import redirect_allowed
from knowledge_hub.oauth.store import OAuthStore
from knowledge_hub.oauth.tokens import (
    ACCESS_PREFIX,
    REFRESH_PREFIX,
    new_secret,
    new_token,
    token_hash,
)
from knowledge_hub.oauth.upstream import UpstreamClient
from knowledge_hub.settings import Settings

DEFAULT_SCOPES = ["kh:read", "kh:write"]
PENDING_SECONDS = 600
REFRESH_GRACE_SECONDS = 10
_GRANT_TYPES = {"authorization_code", "refresh_token"}


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


class HubAuthProvider:
    def __init__(
        self,
        store: OAuthStore,
        upstream: UpstreamClient,
        settings: Settings,
        config: KhConfig,
        membership: MembershipView,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._upstream = upstream
        self._settings = settings
        self._config = config
        self._membership = membership
        self._now = now

    @property
    def _resource_url(self) -> str:
        url = self._settings.resource_url
        assert url is not None, "OAuth is off"
        return url

    def _allowed(self, uri: object) -> bool:
        return redirect_allowed(str(uri), self._settings.redirect_allowlist_tuple)

    # --- clients ----------------------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = await self._store.client_get(client_id)
        if info is None:
            return None
        return OAuthClientInformationFull.model_validate(info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = client_info.redirect_uris or []
        if not uris or not all(self._allowed(u) for u in uris):
            raise RegistrationError("invalid_redirect_uri", "redirect_uri is not allowed")
        if client_info.token_endpoint_auth_method != "none":
            raise RegistrationError("invalid_client_metadata", "token_endpoint_auth_method must be none")
        if set(client_info.grant_types) != _GRANT_TYPES:
            raise RegistrationError(
                "invalid_client_metadata", "grant_types must be authorization_code and refresh_token"
            )
        if client_info.client_id is None:
            raise RegistrationError("invalid_client_metadata", "missing client_id")
        info = client_info.model_dump(mode="json")
        stored = await self._store.client_put(client_info.client_id, info, self._settings.oauth_max_clients)
        if not stored:
            raise RegistrationError("invalid_client_metadata", "too many registered clients")

    # --- authorize --------------------------------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self._allowed(params.redirect_uri):
            raise AuthorizeError("invalid_request", "redirect_uri is not allowed")
        resource = self._resource_url
        if params.resource is not None and params.resource != resource:
            raise AuthorizeError("invalid_target", f"resource must be {resource}")
        request: dict[str, Any] = {
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "state": params.state,
            "scopes": list(params.scopes) if params.scopes else list(DEFAULT_SCOPES),
            "code_challenge": params.code_challenge,
            "resource": resource,
        }
        state, nonce = new_secret(), new_secret()
        verifier, challenge = self._upstream.new_pkce()
        await self._store.pending_put(
            token_hash(state),
            "upstream",
            client.client_id or "",
            request,
            nonce,
            verifier,
            _dt(self._now() + PENDING_SECONDS),
        )
        return await self._upstream.authorization_url(state, nonce, challenge)

    # --- codes and tokens -------------------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = await self._store.code_take(token_hash(authorization_code))
        if row is None or row.client_id != client.client_id:
            return None
        p = row.params
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(p["scopes"]),
            expires_at=row.expires_at.timestamp(),
            client_id=row.client_id,
            code_challenge=p["code_challenge"],
            redirect_uri=AnyUrl(p["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(p["redirect_uri_provided_explicitly"]),
            resource=p.get("resource"),
            subject=row.email,
        )

    async def _mint(
        self,
        client_id: str,
        email: str,
        scopes: list[str],
        resource: str,
        family_id: uuid.UUID,
        family_started: datetime,
    ) -> OAuthToken:
        now = self._now()
        access_ttl = self._settings.oauth_access_ttl_seconds
        refresh_days = self._settings.oauth_refresh_ttl_days
        refresh_cap = family_started.timestamp() + refresh_days * 86400
        access, refresh = new_token(ACCESS_PREFIX), new_token(REFRESH_PREFIX)
        await self._store.token_put(
            token_hash(access), "access", family_id, family_started, client_id, email, scopes, resource,
            _dt(now + access_ttl),
        )  # fmt: skip
        await self._store.token_put(
            token_hash(refresh), "refresh", family_id, family_started, client_id, email, scopes, resource,
            _dt(min(now + refresh_days * 86400, refresh_cap)),
        )  # fmt: skip
        await self._store.client_touch(client_id)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=access_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if client.client_id is None or authorization_code.client_id != client.client_id:
            raise TokenError("invalid_grant", "authorization code was not issued to this client")
        if not authorization_code.subject:
            raise TokenError("invalid_grant", "authorization code has no subject")
        return await self._mint(
            client.client_id,
            authorization_code.subject,
            list(authorization_code.scopes),
            authorization_code.resource or self._resource_url,
            uuid.uuid4(),
            _dt(self._now()),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = await self._store.token_get(token_hash(refresh_token), "refresh")
        if row is None or row.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row.client_id,
            scopes=list(row.scopes),
            expires_at=int(row.expires_at.timestamp()),
            resource=row.resource,
            subject=row.email,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        invalid = TokenError("invalid_grant")
        if client.client_id is None or refresh_token.client_id != client.client_id:
            raise invalid
        digest = token_hash(refresh_token.token)
        outcome = await self._store.refresh_use(digest, REFRESH_GRACE_SECONDS)
        row = await self._store.token_get(digest, "refresh")
        if outcome == "reuse":
            if row is not None:
                await self._store.family_revoke(row.family_id)
            raise invalid
        if outcome != "ok" or row is None:
            raise invalid  # ``reuse_grace`` changes nothing; ``gone`` has nothing to change
        ttl = self._settings.oauth_refresh_ttl_days * 86400
        audiences = self._membership.current().audiences_for_email(row.email, self._config.is_active)
        if not audiences or row.family_started_at.timestamp() + ttl <= self._now():
            await self._store.family_revoke(row.family_id)
            raise invalid
        return await self._mint(
            row.client_id, row.email, list(scopes), row.resource, row.family_id, row.family_started_at
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await self._store.token_get(token_hash(token), "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row.client_id,
            scopes=list(row.scopes),
            expires_at=int(row.expires_at.timestamp()),
            resource=row.resource,
            subject=row.email,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self._store.token_revoke(token_hash(token.token))
