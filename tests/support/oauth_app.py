"""The real app with OAuth on over the migrated database, driven end to end (plan 58, 3.16).

``oauth_stack`` builds the app through ``build_deps`` with the fake IdP of ``tests/support/fake_idp.py`` as
the only upstream and a ``FakeProvider`` as the directory. ``keys_stack`` is the same app with OAuth switched
off. Nothing here reaches a real network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import psycopg
import pytest

from knowledge_hub.app import build_app
from knowledge_hub.auth.keys import create_key
from knowledge_hub.config import (
    DirectoryConfig,
    GroupConfig,
    GroupDirectoryConfig,
    KhConfig,
)
from knowledge_hub.deps import build_deps, close_deps
from knowledge_hub.directory import sync as directory_sync
from knowledge_hub.directory.model import DirectoryUser, GroupFetch
from knowledge_hub.directory.provider import GroupRefs
from knowledge_hub.mcp.server import Deps
from knowledge_hub.settings import Settings
from tests.support.bearer import rpc_body
from tests.support.fake_idp import make_fake_idp
from tests.support.mcp_client import PROTOCOL_VERSION, McpClient, ToolResult
from tests.support.pg import FRESH_DB

PUBLIC_HOST = "hub.test.example"
PUBLIC_URL = f"https://{PUBLIC_HOST}"
RESOURCE = f"{PUBLIC_URL}/mcp"
ISSUER = "https://idp.test.example"
IDP_CLIENT_ID = "kh-hub"
IDP_CLIENT_SECRET = "idp-secret"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
COOKIE = "__Host-kh_flow"
SCOPE = "kh:read kh:write"

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def person(email: str, enabled: bool = True) -> DirectoryUser:
    return DirectoryUser(email, enabled, True, email.split("@", 1)[0])


class FakeProvider:
    """A ``DirectoryProvider`` whose groups the test edits: ``groups`` maps a reference to its users."""

    name = "fake"

    def __init__(self) -> None:
        self.groups: dict[str, list[DirectoryUser]] = {
            "all-g": [person("alice@example.test"), person("dana@example.test", enabled=False)],
            "mgmt-g": [person("bob@example.test")],
            "hr-g": [person("carol@example.test")],
        }

    def remove(self, ref: str, email: str) -> None:
        self.groups[ref] = [u for u in self.groups[ref] if u.email != email]

    async def fetch(self, refs: Mapping[str, GroupRefs]) -> dict[str, GroupFetch]:
        def users(names: tuple[str, ...]) -> tuple[DirectoryUser, ...]:
            return tuple(u for name in names for u in self.groups.get(name, []))

        return {
            g: GroupFetch(members=users(r.members), approvers=users(r.approvers)) for g, r in refs.items()
        }

    async def aclose(self) -> None:
        pass


def stack_config() -> KhConfig:
    """Groups ``all``, ``mgmt`` and ``hr`` as the seed has them; ``sam`` is in ``all`` by ``people`` only."""

    def group(ref: str, *, members: list[str] | None = None) -> GroupConfig:
        return GroupConfig(
            members=members or [],
            approvers=["sam"],
            directory={"fake": GroupDirectoryConfig(members=[ref])},
        )

    return KhConfig(
        people={"sam": "sam@example.test"},
        groups={
            "all": group("all-g", members=["sam"]),
            "mgmt": group("mgmt-g"),
            "hr": group("hr-g"),
        },
        directory=DirectoryConfig(provider="fake", domains=["example.test"]),
    )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


@dataclass
class Callback:
    """What the hub answered to the upstream callback, and the callback URL itself (for replays)."""

    url: str
    response: httpx.Response

    @property
    def location(self) -> str:
        return self.response.headers.get("location", "")


@dataclass
class SignIn:
    client_id: str
    redirect_uri: str
    verifier: str
    state: str
    location: str  # where the browser ends up: the client redirect URI with ``code`` or ``error``
    consent_shown: bool

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.location).query)

    @property
    def code(self) -> str | None:
        return (self.query.get("code") or [None])[0]

    @property
    def error(self) -> str | None:
        return (self.query.get("error") or [None])[0]


class OAuthStack:
    def __init__(
        self,
        *,
        settings: Settings,
        config: KhConfig,
        deps: Deps,
        hub: httpx.AsyncClient,
        idp: httpx.AsyncClient,
        idp_state: Any,
        provider: FakeProvider,
        ids: dict[str, dict[str, str]],
        migrator_dsn: str,
    ) -> None:
        self.settings, self.config, self.deps = settings, config, deps
        self.hub, self.idp, self.idp_state, self.provider = hub, idp, idp_state, provider
        self.ids = ids
        self.docs = ids["documents"]
        self.migrator_dsn = migrator_dsn
        self.mcp = McpClient(hub, None)  # type: ignore[arg-type]  # only ``post`` and ``_result`` are used

    # --- database and directory ---------------------------------------------------------------------------

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.migrator_dsn, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else []

    async def sync(self) -> None:
        """One directory run, then the serving process reads the new snapshot."""
        await directory_sync.sync_once(
            self.config, self.settings, self.provider, self.deps.indexer_pool, accept_change=True
        )
        await self.deps.membership.refresh()

    async def create_key(self, group: str, tools: tuple[str, ...], name: str = "wf") -> str:
        async with await psycopg.AsyncConnection.connect(self.migrator_dsn, autocommit=True) as conn:
            return await create_key(conn, group=group, tools=tools, name=name, created_by="test")

    def revoke_key(self, key: str) -> None:
        key_id = key.split(".", 1)[0].removeprefix("khk_")
        self.sql("UPDATE kh.api_keys SET revoked_at = now() WHERE id = %s", (key_id,))

    # --- the OAuth flow, one step at a time ---------------------------------------------------------------

    async def register(self, redirect_uri: str) -> httpx.Response:
        return await self.hub.post(
            "/register",
            json={
                "redirect_uris": [redirect_uri],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "Claude",
                "scope": SCOPE,
            },
        )

    async def register_client(self, redirect_uri: str = CLAUDE_REDIRECT) -> str:
        resp = await self.register(redirect_uri)
        assert resp.status_code == 201, resp.text
        return resp.json()["client_id"]

    async def authorize(
        self,
        client_id: str,
        redirect_uri: str,
        challenge: str,
        *,
        state: str = "st-1",
        resource: str | None = RESOURCE,
    ) -> httpx.Response:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": SCOPE,
        }
        if resource is not None:
            params["resource"] = resource
        return await self.hub.get("/authorize", params=params)

    async def upstream(self, authorized: httpx.Response, email: str | None = None) -> Callback:
        """Follow ``/authorize`` to the fake IdP (signing in as ``email``) and back to ``/oauth/callback``."""
        assert authorized.status_code == 302, authorized.text
        if email is not None:
            self.idp_state.claims["email"] = email
        at_idp = await self.idp.get(authorized.headers["location"])
        assert at_idp.status_code == 302, at_idp.text
        return await self.callback(at_idp.headers["location"])

    async def callback(self, url: str) -> Callback:
        self.hub.cookies.clear()
        return Callback(url, await self.hub.get(url))

    @staticmethod
    def ticket_of(resp: httpx.Response) -> str | None:
        for header in resp.headers.get_list("set-cookie"):
            found = re.match(rf"{re.escape(COOKIE)}=([^;]+)", header)
            if found and found.group(1) != '""':
                return found.group(1)
        return None

    async def consent_page(self, ticket: str | None) -> httpx.Response:
        self.hub.cookies.clear()
        headers = {"cookie": f"{COOKIE}={ticket}"} if ticket else {}
        return await self.hub.get("/oauth/consent", headers=headers)

    async def consent(self, ticket: str | None, csrf: str, decision: str = "allow") -> httpx.Response:
        self.hub.cookies.clear()
        headers = {"content-type": "application/x-www-form-urlencoded"}
        if ticket:
            headers["cookie"] = f"{COOKIE}={ticket}"
        body = urlencode({"csrf": csrf, "decision": decision})
        return await self.hub.post("/oauth/consent", content=body, headers=headers)

    async def sign_in(
        self,
        email: str,
        *,
        client_id: str | None = None,
        redirect_uri: str = CLAUDE_REDIRECT,
        state: str = "st-1",
        decision: str = "allow",
    ) -> SignIn:
        """/authorize, the fake IdP, /oauth/callback and (when asked) /oauth/consent with cookie and csrf."""
        client_id = client_id or await self.register_client(redirect_uri)
        verifier, challenge = pkce()
        authorized = await self.authorize(client_id, redirect_uri, challenge, state=state)
        called = await self.upstream(authorized, email)
        assert called.response.status_code == 302, called.response.text
        location, shown = called.location, False
        if urlsplit(location).path == "/oauth/consent":
            ticket = self.ticket_of(called.response)
            assert ticket, "the callback set no consent cookie"
            page = await self.consent_page(ticket)
            assert page.status_code == 200, page.text
            csrf = CSRF_RE.search(page.text)
            assert csrf, "the consent page has no csrf field"
            done = await self.consent(ticket, csrf.group(1), decision)
            assert done.status_code == 302, done.text
            location, shown = done.headers["location"], True
        return SignIn(client_id, redirect_uri, verifier, state, location, shown)

    async def token(self, form: dict[str, str]) -> httpx.Response:
        return await self.hub.post("/token", data=form)

    async def exchange(
        self,
        signed: SignIn,
        *,
        client_id: str | None = None,
        redirect_uri: str | None = None,
        verifier: str | None = None,
        code: str | None = None,
    ) -> httpx.Response:
        return await self.token(
            {
                "grant_type": "authorization_code",
                "code": code or signed.code or "",
                "redirect_uri": redirect_uri or signed.redirect_uri,
                "client_id": client_id or signed.client_id,
                "code_verifier": verifier or signed.verifier,
                "resource": RESOURCE,
            }
        )

    async def tokens(self, signed: SignIn) -> dict[str, Any]:
        resp = await self.exchange(signed)
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def refresh(self, client_id: str, refresh_token: str) -> httpx.Response:
        return await self.token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
        )

    async def revoke(self, client_id: str, token: str) -> httpx.Response:
        # The SDK's RevocationRequest declares `client_secret: str | None` with no default, so the
        # field must be present even for a public client (auth method "none"); an empty value is
        # accepted and ignored by the client authenticator.
        return await self.hub.post(
            "/revoke", data={"token": token, "client_id": client_id, "client_secret": ""}
        )

    async def login(self, email: str) -> tuple[SignIn, dict[str, Any]]:
        """A complete login: returns the sign-in and the token response."""
        signed = await self.sign_in(email)
        assert signed.code, signed.location
        return signed, await self.tokens(signed)

    # --- /mcp ---------------------------------------------------------------------------------------------

    async def mcp_post(self, token: str | None, body: bytes) -> httpx.Response:
        return await self.mcp.post(body, token)

    async def mcp_call(
        self, token: str | None, tool: str, args: dict[str, Any] | None = None
    ) -> ToolResult:
        init = rpc_body(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
            id=0,
        )
        first = await self.mcp.post(init, token)
        if first.status_code != 200:
            return ToolResult(first.status_code, True, first.text)
        note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
        await self.mcp.post(note, token)
        body = rpc_body("tools/call", {"name": tool, "arguments": args or {}}, id=1)
        return McpClient._result(await self.mcp.post(body, token))

    async def search_ids(self, token: str) -> list[str]:
        """The ids ``search`` returns for the word every seed chunk holds."""
        res = await self.mcp_call(token, "search", {"query": "chunk", "min_tier": "T0"})
        assert not res.is_error, res.text
        return [h["id"] for h in res.data["hits"]]

    def doc(self, group: str) -> str:
        return f"doc:{self.docs[group]}"


async def _boot(
    db_settings: Settings, fresh_db: Any, ids: dict[str, dict[str, str]], tmp_path: Any, *, oauth: bool
):
    cursor_key = tmp_path / "cursor.key"
    cursor_key.write_bytes(b"k" * 32)
    update: dict[str, Any] = {
        "env": "test",
        "allowed_hosts": PUBLIC_HOST,
        "embed_provider": "fake",
        "cursor_key_file": cursor_key,
    }
    idp_app = make_fake_idp(ISSUER, IDP_CLIENT_ID, IDP_CLIENT_SECRET)
    idp = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp_app))
    if oauth:
        secret = tmp_path / "oidc.secret"
        secret.write_text(IDP_CLIENT_SECRET)
        update.update(
            public_url=PUBLIC_URL,
            oidc_issuer=ISSUER,
            oidc_client_id=IDP_CLIENT_ID,
            oidc_client_secret_file=secret,
            oidc_allowed_domains="example.test",
        )
    settings = db_settings.model_copy(update=update)
    assert settings.oauth_enabled is oauth
    config = stack_config()
    deps = await build_deps(settings, config, upstream_http=idp)
    app = build_app(settings, deps)
    started, stop = asyncio.Event(), asyncio.Event()

    # With a public URL ``build_app`` wraps the Starlette app in the host guard, which has no router.
    inner = getattr(app, "app", app)

    async def lifespan() -> None:
        async with inner.router.lifespan_context(inner):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    waiter = asyncio.create_task(started.wait())
    # Wake when the lifespan task dies too, so its exception shows instead of a bare timeout.
    await asyncio.wait({task, waiter}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    if not started.is_set():
        waiter.cancel()
        if task.done():
            task.result()
        raise TimeoutError("the app lifespan did not start")
    hub = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL)
    stack = OAuthStack(
        settings=settings,
        config=config,
        deps=deps,
        hub=hub,
        idp=idp,
        idp_state=idp_app.state.idp,
        provider=FakeProvider(),
        ids=ids,
        migrator_dsn=fresh_db.dsn("kh_migrator", FRESH_DB),
    )

    async def teardown() -> None:
        stop.set()
        await task
        await hub.aclose()
        await close_deps(deps)
        await idp.aclose()

    return stack, teardown


@pytest.fixture
async def oauth_stack(db_settings, fresh_db, seeded_db, tmp_path):
    stack, teardown = await _boot(db_settings, fresh_db, seeded_db, tmp_path, oauth=True)
    try:
        await stack.sync()
        yield stack
    finally:
        await teardown()


@pytest.fixture
async def keys_stack(db_settings, fresh_db, seeded_db, tmp_path):
    stack, teardown = await _boot(db_settings, fresh_db, seeded_db, tmp_path, oauth=False)
    try:
        yield stack
    finally:
        await teardown()
