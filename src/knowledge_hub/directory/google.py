"""Google Workspace directory provider (plan 58, 3.5): Directory API with domain-wide delegation.

No Google client library: a signed JWT assertion is exchanged for a token with httpx.
Every ProviderError text holds the endpoint name and the status code only.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
import jwt

from knowledge_hub.directory.model import DirectoryUser, GroupFetch
from knowledge_hub.directory.provider import GroupRefs, ProviderError

if TYPE_CHECKING:
    from knowledge_hub.settings import Settings

TOKEN_URI = "https://oauth2.googleapis.com/token"
API = "https://admin.googleapis.com/admin/directory/v1"
SCOPES = (
    "https://www.googleapis.com/auth/admin.directory.group.member.readonly",
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
)
REQUIRED = "google: KH_DIRECTORY_GOOGLE_CREDENTIALS_FILE and KH_DIRECTORY_GOOGLE_SUBJECT are required"
WAITS = (1, 3)
DEADLINE_SECONDS = 60
REFRESH_MARGIN = 300
ASSERTION_TTL = 3600


def make(settings: Settings, http: httpx.AsyncClient | None) -> GoogleProvider:
    return GoogleProvider(
        settings.directory_google_credentials_file, settings.directory_google_subject, http
    )


class GoogleProvider:
    name = "google"

    def __init__(
        self,
        credentials_file: Any,
        subject: str | None,
        http: httpx.AsyncClient | None = None,
        *,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not credentials_file or not subject:
            raise ProviderError(REQUIRED)
        try:
            with open(credentials_file, encoding="utf-8") as fh:
                creds = json.load(fh)
            self._email = str(creds["client_email"])
            self._key = str(creds["private_key"])
            token_uri = str(creds["token_uri"])
        except (OSError, ValueError, KeyError, TypeError):
            raise ProviderError("google: credentials file is unreadable or incomplete") from None
        if token_uri != TOKEN_URI:
            raise ProviderError("google: token_uri must be " + TOKEN_URI)
        self._subject = subject
        self._domain = subject.rsplit("@", 1)[-1].lower() if "@" in subject else ""
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=30)
        self._now = now
        self._sleep = sleep
        self._token: str | None = None
        self._token_expiry = 0.0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- transport ---------------------------------------------------------------

    async def _request(self, endpoint: str, method: str, url: str, **kw: Any) -> dict[str, Any]:
        """One call with retry of 429 and 5xx: three tries, waits 1 s then 3 s, deadline 60 s."""
        start = self._now()
        for attempt in range(len(WAITS) + 1):
            try:
                resp = await self._http.request(method, url, **kw)
            except httpx.HTTPError:
                raise ProviderError(f"google {endpoint}: request failed") from None
            status = resp.status_code
            if status == 200:
                try:
                    data = resp.json()
                except ValueError:
                    raise ProviderError(f"google {endpoint}: malformed response") from None
                if not isinstance(data, dict):
                    raise ProviderError(f"google {endpoint}: malformed response")
                return data
            retryable = status == 429 or status >= 500
            if not retryable or attempt == len(WAITS):
                raise ProviderError(f"google {endpoint}: status {status}")
            wait = WAITS[attempt]
            if self._now() - start + wait > DEADLINE_SECONDS:
                raise ProviderError(f"google {endpoint}: status {status}")
            await self._sleep(wait)
        raise ProviderError(f"google {endpoint}: failed")  # pragma: no cover

    async def _bearer(self) -> str:
        now = self._now()
        if self._token and now < self._token_expiry - REFRESH_MARGIN:
            return self._token
        assertion = jwt.encode(
            {
                "iss": self._email,
                "sub": self._subject,
                "scope": " ".join(SCOPES),
                "aud": TOKEN_URI,
                "iat": int(now),
                "exp": int(now) + ASSERTION_TTL,
            },
            self._key,
            algorithm="RS256",
        )
        data = await self._request(
            "token",
            "POST",
            TOKEN_URI,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )
        token = data.get("access_token")
        expires = data.get("expires_in", ASSERTION_TTL)
        bad_expiry = isinstance(expires, bool) or not isinstance(expires, int)
        if not isinstance(token, str) or not token or bad_expiry:
            raise ProviderError("google token: malformed response")
        self._token = token
        self._token_expiry = now + expires
        return token

    async def _pages(self, endpoint: str, url: str, params: dict[str, str], key: str) -> list[Any]:
        rows: list[Any] = []
        page_token: str | None = None
        while True:
            query = dict(params)
            if page_token:
                query["pageToken"] = page_token
            headers = {"Authorization": f"Bearer {await self._bearer()}"}
            data = await self._request(endpoint, "GET", url, params=query, headers=headers)
            page = data.get(key, [])
            if not isinstance(page, list):
                raise ProviderError(f"google {endpoint}: malformed response")
            rows.extend(page)
            nxt = data.get("nextPageToken")
            if nxt is None or nxt == "":
                return rows
            if not isinstance(nxt, str):
                raise ProviderError(f"google {endpoint}: malformed response")
            page_token = nxt

    # -- fetch -------------------------------------------------------------------

    async def _members(self, ref: str) -> list[str]:
        url = f"{API}/groups/{quote(ref, safe='')}/members"
        rows = await self._pages(
            "members", url, {"includeDerivedMembership": "true", "maxResults": "200"}, "members"
        )
        out: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ProviderError("google members: malformed response")
            kind = row.get("type")
            if kind == "GROUP":
                continue
            if kind == "CUSTOMER":
                raise ProviderError(f"group {ref} contains the whole domain")
            email = row.get("email")
            if kind != "USER" or not isinstance(email, str) or "@" not in email:
                raise ProviderError("google members: malformed response")
            if row.get("status") == "ACTIVE":
                out.append(email.lower())
            else:
                out.append("!" + email.lower())
        return out

    async def _users(self) -> dict[str, bool]:
        rows = await self._pages(
            "users",
            f"{API}/users",
            {
                "customer": "my_customer",
                "maxResults": "500",
                "fields": "nextPageToken,users(primaryEmail,suspended,archived,aliases,nonEditableAliases)",
            },
            "users",
        )
        enabled: dict[str, bool] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("primaryEmail"), str):
                raise ProviderError("google users: malformed response")
            ok = row.get("suspended") is not True and row.get("archived") is not True
            addrs = [row["primaryEmail"], *(row.get("aliases") or []), *(row.get("nonEditableAliases") or [])]
            for addr in addrs:
                if not isinstance(addr, str):
                    raise ProviderError("google users: malformed response")
                enabled[addr.lower()] = ok
        return enabled

    def _user(self, entry: str, users: Mapping[str, bool]) -> DirectoryUser:
        active = not entry.startswith("!")
        email = entry.lstrip("!")
        if email.rsplit("@", 1)[-1] == self._domain:
            active = active and users.get(email, False)
        return DirectoryUser(email=email, enabled=active, email_verified=True)

    async def fetch(self, refs: Mapping[str, GroupRefs]) -> dict[str, GroupFetch]:
        wanted = sorted({r for g in refs.values() for r in (*g.members, *g.approvers)})
        listed = {ref: await self._members(ref) for ref in wanted}
        users = await self._users()

        def build(group_refs: tuple[str, ...]) -> tuple[DirectoryUser, ...]:
            seen: dict[str, DirectoryUser] = {}
            for ref in group_refs:
                for entry in listed[ref]:
                    user = self._user(entry, users)
                    prev = seen.get(user.email)
                    seen[user.email] = user if prev is None else (prev if prev.enabled else user)
            return tuple(seen.values())

        return {name: GroupFetch(build(g.members), build(g.approvers)) for name, g in refs.items()}
