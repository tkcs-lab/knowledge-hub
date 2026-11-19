"""Keycloak directory provider (plan 58, 3.5): admin REST API with a client-credentials token.

Every ProviderError text holds the endpoint name and the status code only.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx

from knowledge_hub.directory.model import DirectoryUser, GroupFetch
from knowledge_hub.directory.provider import GroupRefs, ProviderError

if TYPE_CHECKING:
    from knowledge_hub.settings import Settings

REQUIRED = "keycloak: KH_DIRECTORY_KEYCLOAK_URL, _REALM, _CLIENT_ID and _CLIENT_SECRET_FILE are required"
NOT_HTTPS = "keycloak: KH_DIRECTORY_KEYCLOAK_URL must be https"
PATH_REF = re.compile(r"^/[A-Za-z0-9 _-]{1,100}(/[A-Za-z0-9 _-]{1,100})*$")
NAME_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,100}$")
WAITS = (1, 3)
DEADLINE_SECONDS = 60
PAGE = 100
MAX_DEPTH = 10
REFRESH_MARGIN = 30


def make(settings: Settings, http: httpx.AsyncClient | None) -> KeycloakProvider:
    url = settings.directory_keycloak_url
    realm = settings.directory_keycloak_realm
    client_id = settings.directory_keycloak_client_id
    secret_file = settings.directory_keycloak_client_secret_file
    if not url or not realm or not client_id or not secret_file:
        raise ProviderError(REQUIRED)
    try:
        with open(secret_file, encoding="utf-8") as fh:
            secret = fh.read().strip()
    except OSError:
        raise ProviderError("keycloak: client secret file is unreadable") from None
    if not secret:
        raise ProviderError(REQUIRED)
    return KeycloakProvider(
        url, realm, client_id, secret, settings.directory_keycloak_subgroups, http, env=settings.env
    )


def _check_url(url: str, env: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        raise ProviderError(NOT_HTTPS)
    if parts.scheme == "http" and env not in ("dev", "test") and "." in host:
        raise ProviderError(NOT_HTTPS)
    return url.rstrip("/")


class KeycloakProvider:
    name = "keycloak"

    def __init__(
        self,
        url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        subgroups: bool,
        http: httpx.AsyncClient | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
        env: str = "prod",
    ) -> None:
        if not url or not realm or not client_id or not client_secret:
            raise ProviderError(REQUIRED)
        self._base = _check_url(url, env)
        self._realm = quote(realm, safe="")
        self._client_id = client_id
        self._secret = client_secret
        self._subgroups = subgroups
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=30)
        self._sleep = sleep
        self._now = now
        self._token: str | None = None
        self._token_expiry = 0.0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- transport ---------------------------------------------------------------

    async def _request(self, endpoint: str, method: str, url: str, **kw: Any) -> Any:
        """One call with retry of 429 and 5xx: three tries, waits 1 s then 3 s, deadline 60 s."""
        start = self._now()
        for attempt in range(len(WAITS) + 1):
            try:
                resp = await self._http.request(method, url, **kw)
            except httpx.HTTPError:
                raise ProviderError(f"keycloak {endpoint}: request failed") from None
            status = resp.status_code
            if status == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise ProviderError(f"keycloak {endpoint}: malformed response") from None
            retryable = status == 429 or status >= 500
            if not retryable or attempt == len(WAITS):
                raise ProviderError(f"keycloak {endpoint}: status {status}")
            wait = WAITS[attempt]
            if self._now() - start + wait > DEADLINE_SECONDS:
                raise ProviderError(f"keycloak {endpoint}: status {status}")
            await self._sleep(wait)
        raise ProviderError(f"keycloak {endpoint}: failed")  # pragma: no cover

    async def _bearer(self) -> str:
        now = self._now()
        if self._token and now < self._token_expiry - REFRESH_MARGIN:
            return self._token
        data = await self._request(
            "token",
            "POST",
            f"{self._base}/realms/{self._realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._secret,
            },
        )
        token = data.get("access_token") if isinstance(data, dict) else None
        expires = data.get("expires_in", 60) if isinstance(data, dict) else None
        if (
            not isinstance(token, str)
            or not token
            or isinstance(expires, bool)
            or not isinstance(expires, int)
        ):
            raise ProviderError("keycloak token: malformed response")
        self._token = token
        self._token_expiry = now + expires
        return token

    async def _get(self, endpoint: str, path: str, params: Mapping[str, str] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {await self._bearer()}"}
        url = f"{self._base}/admin/realms/{self._realm}/{path}"
        return await self._request(endpoint, "GET", url, params=params, headers=headers)

    async def _paged(self, endpoint: str, path: str, extra: Mapping[str, str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        first = 0
        while True:
            page = await self._get(endpoint, path, {**extra, "first": str(first), "max": str(PAGE)})
            if not isinstance(page, list) or any(not isinstance(r, dict) for r in page):
                raise ProviderError(f"keycloak {endpoint}: malformed response")
            rows.extend(page)
            if len(page) < PAGE:
                return rows
            first += PAGE

    # -- groups ------------------------------------------------------------------

    async def _group_id(self, ref: str) -> str:
        if ref.startswith("/"):
            if not PATH_REF.match(ref):
                raise ProviderError("keycloak: invalid group reference")
            path = "/".join(quote(seg, safe="") for seg in ref.split("/")[1:])
            try:
                data = await self._get("group-by-path", f"group-by-path/{path}")
            except ProviderError as exc:
                if str(exc).endswith("status 404"):
                    raise ProviderError("keycloak group-by-path: group not found") from None
                raise
            found = data if isinstance(data, dict) else None
        else:
            if not NAME_REF.match(ref):
                raise ProviderError("keycloak: invalid group reference")
            data = await self._get("groups", "groups", {"search": ref, "exact": "true"})
            if not isinstance(data, list):
                raise ProviderError("keycloak groups: malformed response")
            found = next((g for g in data if isinstance(g, dict) and g.get("path") == "/" + ref), None)
        gid = found.get("id") if found else None
        if not isinstance(gid, str) or not gid:
            raise ProviderError("keycloak groups: group not found")
        return gid

    async def _direct_members(self, gid: str) -> list[dict[str, Any]]:
        return await self._paged(
            "members", f"groups/{quote(gid, safe='')}/members", {"briefRepresentation": "false"}
        )

    async def _members(self, ref: str) -> tuple[DirectoryUser, ...]:
        root = await self._group_id(ref)
        rows: dict[str, dict[str, Any]] = {}
        visited: set[str] = set()
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            gid, depth = stack.pop()
            if gid in visited:
                continue
            if depth > MAX_DEPTH:
                raise ProviderError("keycloak children: nesting too deep")
            visited.add(gid)
            for row in await self._direct_members(gid):
                uid = row.get("id")
                rows[uid if isinstance(uid, str) and uid else f"#{len(rows)}"] = row
            if self._subgroups:
                children = await self._paged("children", f"groups/{quote(gid, safe='')}/children", {})
                for child in children:
                    cid = child.get("id")
                    if not isinstance(cid, str) or not cid:
                        raise ProviderError("keycloak children: malformed response")
                    stack.append((cid, depth + 1))
        return tuple(self._user(r) for r in rows.values())

    @staticmethod
    def _user(row: Mapping[str, Any]) -> DirectoryUser:
        email = row.get("email")
        username = row.get("username")
        return DirectoryUser(
            email=email.strip().lower() if isinstance(email, str) else "",
            enabled=row.get("enabled") is True,
            email_verified=row.get("emailVerified") is True,
            username=username if isinstance(username, str) else "",
        )

    async def fetch(self, refs: Mapping[str, GroupRefs]) -> dict[str, GroupFetch]:
        wanted = sorted({r for g in refs.values() for r in (*g.members, *g.approvers)})
        for ref in wanted:  # refuse a bad reference before any request
            ok = PATH_REF.match(ref) if ref.startswith("/") else NAME_REF.match(ref)
            if not ok:
                raise ProviderError("keycloak: invalid group reference")
        listed = {ref: await self._members(ref) for ref in wanted}

        def build(group_refs: tuple[str, ...]) -> tuple[DirectoryUser, ...]:
            seen: dict[tuple[str, str], DirectoryUser] = {}
            for ref in group_refs:
                for user in listed[ref]:
                    seen.setdefault((user.email, user.username), user)
            return tuple(seen.values())

        return {name: GroupFetch(build(g.members), build(g.approvers)) for name, g in refs.items()}
