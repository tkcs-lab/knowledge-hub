"""The authorization flow behind ``/oauth/callback`` and ``/oauth/consent``: no HTTP, no cookies."""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import construct_redirect_uri

from knowledge_hub.oauth.allowlist import redirect_allowed
from knowledge_hub.oauth.tokens import new_secret, token_hash
from knowledge_hub.oauth.upstream import UpstreamError

log = logging.getLogger(__name__)

TICKET_TTL_SECONDS = 600
CODE_TTL_SECONDS = 60
CLIENT_NAME_MAX = 100


class FlowError(Exception):
    """A refusal the route answers with 400 ``invalid request``."""


@dataclass(frozen=True)
class CallbackOutcome:
    redirect_url: str | None = None
    consent_ticket: str | None = None
    csrf: str | None = None


@dataclass(frozen=True)
class ConsentContext:
    client_name: str
    redirect_host: str
    redirect_origin: str
    email: str
    csrf: str


class AuthorizationFlow:
    def __init__(
        self,
        store: Any,
        upstream: Any,
        settings: Any,
        config: Any,
        membership: Any,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._upstream = upstream
        self._settings = settings
        self._config = config
        self._membership = membership
        self._now = now

    def _at(self, seconds: int) -> datetime:
        return datetime.fromtimestamp(self._now(), UTC) + timedelta(seconds=seconds)

    def _allowed(self, request: dict[str, Any]) -> bool:
        uri = request.get("redirect_uri")
        return isinstance(uri, str) and redirect_allowed(uri, self._settings.redirect_allowlist_tuple)

    @staticmethod
    def _denied(request: dict[str, Any]) -> str:
        return construct_redirect_uri(
            request["redirect_uri"], error="access_denied", state=request.get("state")
        )

    def _has_group(self, email: str) -> bool:
        return bool(self._membership.current().audiences_for_email(email, self._config.is_active))

    # --- upstream callback --------------------------------------------------------------------------------

    async def handle_callback(
        self, code: str | None, state: str | None, error: str | None
    ) -> CallbackOutcome:
        if not state:
            raise FlowError("missing state")
        row = await self._store.pending_take(token_hash(state), "upstream")
        if row is None:
            raise FlowError("unknown state")
        request = row.params
        if not self._allowed(request):
            raise FlowError("redirect uri no longer allowed")
        if error or not code:
            return CallbackOutcome(redirect_url=self._denied(request))
        try:
            tokens = await self._upstream.exchange(code, row.verifier)
            id_token = tokens.get("id_token")
            if not isinstance(id_token, str):
                raise UpstreamError("id_token_missing")
            identity = await self._upstream.verify_id_token(id_token, row.nonce)
        except UpstreamError as exc:
            log.warning("upstream refused: %s", exc.code)
            return CallbackOutcome(redirect_url=self._denied(request))
        email = identity.email
        if not self._has_group(email):
            return CallbackOutcome(redirect_url=self._denied(request))
        if await self._store.consent_has(email, row.client_id):
            return CallbackOutcome(redirect_url=await self.issue_code(request, email, row.client_id))
        ticket, csrf = new_secret(), new_secret()
        await self._store.pending_put(
            token_hash(ticket),
            "consent",
            row.client_id,
            {"email": email, "request": request, "csrf": csrf},
            "",
            "",
            self._at(TICKET_TTL_SECONDS),
        )
        return CallbackOutcome(consent_ticket=ticket, csrf=csrf)

    # --- consent ------------------------------------------------------------------------------------------

    async def consent_context(self, ticket: str | None) -> ConsentContext:
        if not ticket:
            raise FlowError("missing ticket")
        row = await self._store.pending_peek(token_hash(ticket), "consent")
        if row is None:
            raise FlowError("unknown ticket")
        request = row.params["request"]
        parts = urlsplit(request["redirect_uri"])
        info = await self._store.client_get(row.client_id) or {}
        name = str(info.get("client_name") or row.client_id)[:CLIENT_NAME_MAX]
        return ConsentContext(
            client_name=name,
            redirect_host=parts.hostname or "",
            redirect_origin=f"{parts.scheme}://{parts.netloc}",
            email=row.params["email"],
            csrf=row.params["csrf"],
        )

    async def decide_consent(self, ticket: str | None, csrf: str | None, allow: bool) -> str:
        if not ticket or not csrf:
            raise FlowError("missing ticket or csrf")
        digest = token_hash(ticket)
        row = await self._store.pending_peek(digest, "consent")
        if row is None:
            raise FlowError("unknown ticket")
        if not hmac.compare_digest(csrf.encode(), str(row.params["csrf"]).encode()):
            raise FlowError("csrf mismatch")
        if await self._store.pending_take(digest, "consent") is None:
            raise FlowError("ticket already used")
        request, email = row.params["request"], row.params["email"]
        if not self._allowed(request):
            raise FlowError("redirect uri no longer allowed")
        if not allow or not self._has_group(email):
            return self._denied(request)
        await self._store.consent_put(email, row.client_id)
        await self._store.client_authorize(row.client_id)
        return await self.issue_code(request, email, row.client_id)

    # --- code ---------------------------------------------------------------------------------------------

    async def issue_code(self, request: dict[str, Any], email: str, client_id: str) -> str:
        code = new_secret()
        await self._store.code_put(token_hash(code), client_id, email, request, self._at(CODE_TTL_SECONDS))
        return construct_redirect_uri(request["redirect_uri"], code=code, state=request.get("state"))
