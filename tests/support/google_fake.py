"""A MockTransport for the Google token endpoint and the Directory API (plan 58, 3.5)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

import httpx

GROUPS_PREFIX = "/admin/directory/v1/groups/"


def make_google_transport(
    groups: Mapping[str, list[list[dict[str, Any]]]],
    users: list[list[dict[str, Any]]],
    *,
    fail: Mapping[str, Any] | None = None,
) -> httpx.MockTransport:
    """groups: key -> list of member pages; users: list of user pages.

    `fail` switches: `not_found` (a group key), `429_once` (True), `500_always` (True),
    `500_members_page2` (True), `500_users_page2` (True), `malformed_members` (True).
    The transport has a `.requests` list of every request.
    """
    fail = dict(fail or {})
    requests: list[httpx.Request] = []
    state = {"limited": False, "tokens": 0}

    def page_of(pages: list[list[dict[str, Any]]], token: str | None, key: str) -> dict[str, Any]:
        idx = int(token) if token else 0
        body: dict[str, Any] = {key: pages[idx]}
        if idx + 1 < len(pages):
            body["nextPageToken"] = str(idx + 1)
        return body

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = request.url
        if fail.get("500_always") and url.host != "oauth2.googleapis.com":
            return httpx.Response(500, text="secret-body")
        if url.host == "oauth2.googleapis.com":
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"tok{state['tokens']}", "expires_in": 3600})
        if request.headers.get("authorization", "").startswith("Bearer tok") is False:
            return httpx.Response(401, text="secret-body")
        token = url.params.get("pageToken")
        if url.path.startswith(GROUPS_PREFIX) and url.path.endswith("/members"):
            key = unquote(url.path[len(GROUPS_PREFIX) : -len("/members")])
            if url.params.get("includeDerivedMembership") != "true":
                return httpx.Response(400, text="secret-body")
            if fail.get("429_once") and not state["limited"]:
                state["limited"] = True
                return httpx.Response(429, text="secret-body")
            if fail.get("not_found") == key or key not in groups:
                return httpx.Response(404, text="secret-body")
            if token and fail.get("500_members_page2"):
                return httpx.Response(500, text="secret-body")
            if fail.get("malformed_members"):
                return httpx.Response(200, json={"members": "nope"})
            return httpx.Response(200, json=page_of(groups[key], token, "members"))
        if url.path == "/admin/directory/v1/users":
            if token and fail.get("500_users_page2"):
                return httpx.Response(500, text="secret-body")
            return httpx.Response(200, content=json.dumps(page_of(users, token, "users")))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    transport.requests = requests  # type: ignore[attr-defined]
    return transport
