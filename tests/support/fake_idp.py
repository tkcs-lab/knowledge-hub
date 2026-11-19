"""A fake OpenID provider for the upstream OIDC client tests, served through ``httpx.ASGITransport``.

``make_fake_idp`` returns a Starlette app. Everything a test edits hangs off ``app.state.idp``: ``claims``
(signed by ``/token``, plus ``nonce`` from the authorize request unless overridden), ``discovery``, ``sign``,
``add_key``, ``jwk_overrides``, ``pad`` and ``hits``.
Create it through a function-scoped fixture so nothing leaks between tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections import Counter
from typing import Any
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

FIXED_CODE = "fixed-code"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forge(header: dict[str, Any], claims: dict[str, Any], key: bytes | None = None) -> str:
    """Hand-build a compact JWT: no signature for ``none``, an HMAC-SHA256 with ``key`` for ``HS256``."""
    head = b64url(json.dumps(header).encode())
    body = b64url(json.dumps(claims).encode())
    signing = f"{head}.{body}"
    if header.get("alg") == "HS256":
        assert key is not None
        return f"{signing}.{b64url(hmac.new(key, signing.encode(), hashlib.sha256).digest())}"
    return f"{signing}."


def make_fake_idp(issuer: str, client_id: str, client_secret: str) -> Starlette:
    issuer = issuer.rstrip("/")
    keys: dict[str, Any] = {
        "rsa-1": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "ec-1": ec.generate_private_key(ec.SECP256R1()),
    }
    served: set[str] = {"rsa-1", "ec-1"}
    pending: dict[str, dict[str, str]] = {}

    state = _State(issuer, client_id, keys, served)

    def jwk_of(kid: str) -> dict[str, Any]:
        public = keys[kid].public_key()
        alg = RSAAlgorithm if isinstance(keys[kid], rsa.RSAPrivateKey) else ECAlgorithm
        jwk = json.loads(alg.to_jwk(public))
        jwk.update({"kid": kid, "use": "sig", "alg": "RS256" if alg is RSAAlgorithm else "ES256"})
        jwk.update(state.jwk_overrides.get(kid, {}))
        return jwk

    def padded(body: dict[str, Any], name: str) -> JSONResponse:
        if name in state.pad:
            body = {**body, "padding": "a" * state.pad[name]}
        return JSONResponse(body)

    async def discovery(request: Request) -> Response:
        state.hits["discovery"] += 1
        return padded(state.discovery, "discovery")

    async def jwks(request: Request) -> Response:
        state.hits["jwks"] += 1
        return padded({"keys": [jwk_of(k) for k in keys if k in served]}, "jwks")

    async def redirect(request: Request) -> Response:
        return RedirectResponse(f"{issuer}/jwks", status_code=302)

    async def authorize(request: Request) -> Response:
        q = request.query_params
        if q.get("client_id") != client_id or q.get("response_type") != "code":
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        pending[FIXED_CODE] = {
            "nonce": q.get("nonce", ""),
            "challenge": q.get("code_challenge", ""),
            "method": q.get("code_challenge_method", ""),
        }
        target = q["redirect_uri"] + "?" + urlencode({"code": FIXED_CODE, "state": q.get("state", "")})
        return RedirectResponse(target, status_code=302)

    async def token(request: Request) -> Response:
        form = await request.form()
        entry = pending.get(str(form.get("code")))
        verifier = str(form.get("code_verifier", ""))
        challenge = b64url(hashlib.sha256(verifier.encode()).digest())
        if (
            form.get("client_id") != client_id
            or form.get("client_secret") != client_secret
            or form.get("grant_type") != "authorization_code"
            or entry is None
            or entry["method"] != "S256"
            or not hmac.compare_digest(challenge, entry["challenge"])
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        now = int(time.time())
        claims = {"iat": now, "exp": now + 300, "nonce": entry["nonce"], **state.claims}
        return padded(
            {"id_token": state.sign(claims), "access_token": "unused", "token_type": "Bearer"}, "token"
        )

    app = Starlette(
        routes=[
            Route("/.well-known/openid-configuration", discovery),
            Route("/jwks", jwks),
            Route("/redirect", redirect),
            Route("/authorize", authorize),
            Route("/token", token, methods=["POST"]),
        ]
    )
    app.state.idp = state
    return app


class _State:
    """What a test edits; reachable as ``app.state.idp``."""

    def __init__(self, issuer: str, client_id: str, keys: dict[str, Any], served: set[str]) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.keys = keys
        self.served = served
        self.hits: Counter[str] = Counter()
        self.pad: dict[str, int] = {}
        self.jwk_overrides: dict[str, dict[str, Any]] = {}
        self.discovery: dict[str, Any] = {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "jwks_uri": f"{issuer}/jwks",
        }
        self.claims: dict[str, Any] = {
            "iss": issuer,
            "aud": client_id,
            "sub": "sub-1",
            "email": "alice@example.test",
            "email_verified": True,
            "hd": "example.test",
        }

    def add_key(self, kid: str) -> None:
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.served.add(kid)

    def public_pem(self, kid: str = "rsa-1") -> bytes:
        return (
            self.keys[kid]
            .public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )

    def sign(self, claims: dict[str, Any], alg: str = "RS256", kid: str | None = None) -> str:
        """Sign exactly ``claims``. The key is the RSA key for RSA and PS algorithms and the EC key for ES256;
        ``kid`` is the header label, so a token can carry any ``kid``."""
        default = "ec-1" if alg.startswith("ES") else "rsa-1"
        key = self.keys.get(kid or default) if (kid or default) in self.keys else self.keys[default]
        if alg.startswith("ES") and not isinstance(key, ec.EllipticCurvePrivateKey):
            key = self.keys["ec-1"]
        if not alg.startswith("ES") and not isinstance(key, rsa.RSAPrivateKey):
            key = self.keys["rsa-1"]
        return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid or default})
