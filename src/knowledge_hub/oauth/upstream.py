"""The hub's OpenID Connect client for the upstream identity provider.

Everything the ID token claims is checked here and nothing is read from any other endpoint: the identity
is the verified ``id_token`` of the code exchange, and only that.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

TIMEOUT_SECONDS = 10.0
MAX_BODY = 1024 * 1024
DISCOVERY_TTL = 3600.0
JWKS_TTL = 300.0
LEEWAY_SECONDS = 60
GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_HOSTS = frozenset({"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com"})
ALGORITHMS = ("RS256", "ES256")
_KTY = {"RS256": "RSA", "ES256": "EC"}


class UpstreamError(Exception):
    """A refusal of the upstream provider or its token; ``code`` is short and safe to log."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class UpstreamConfig:
    issuer: str
    client_id: str
    client_secret: str
    allowed_domains: tuple[str, ...]
    hosted_domains: tuple[str, ...]
    redirect_uri: str
    allow_http: bool = False


@dataclass(frozen=True)
class VerifiedIdentity:
    email: str
    sub: str
    hd: str | None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class UpstreamClient:
    def __init__(
        self, config: UpstreamConfig, http: httpx.AsyncClient, *, now: Callable[[], float] = time.time
    ) -> None:
        self._config = config
        self._http = http
        self._now = now
        self._discovery: dict[str, Any] | None = None
        self._discovery_at = 0.0
        self._jwks: list[dict[str, Any]] | None = None
        self._jwks_at = 0.0

    # --- HTTP -------------------------------------------------------------------------------------------

    def _check_url(self, url: object) -> str:
        if not isinstance(url, str):
            raise UpstreamError("endpoint_invalid")
        parts = urlsplit(url)
        scheme_ok = parts.scheme == "https" or (parts.scheme == "http" and self._config.allow_http)
        if not scheme_ok or not parts.hostname:
            raise UpstreamError("endpoint_scheme")
        host = parts.hostname.lower()
        allowed = {(urlsplit(self._config.issuer).hostname or "").lower()}
        if self._config.issuer == GOOGLE_ISSUER:
            allowed |= GOOGLE_HOSTS
        if host not in allowed:
            raise UpstreamError("endpoint_host")
        return url

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        body = bytearray()
        try:
            async with self._http.stream(
                method, url, follow_redirects=False, timeout=TIMEOUT_SECONDS, **kwargs
            ) as response:
                if response.status_code != 200:
                    raise UpstreamError("upstream_status")
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BODY:
                        raise UpstreamError("response_too_large")
        except httpx.HTTPError as exc:
            raise UpstreamError("upstream_unreachable") from exc
        try:
            data = json.loads(bytes(body))
        except ValueError as exc:
            raise UpstreamError("response_malformed") from exc
        if not isinstance(data, dict):
            raise UpstreamError("response_malformed")
        return data

    # --- Discovery and JWKS ------------------------------------------------------------------------------

    async def discover(self) -> dict[str, Any]:
        if self._discovery is not None and self._now() - self._discovery_at < DISCOVERY_TTL:
            return self._discovery
        issuer = self._config.issuer
        url = self._check_url(issuer.rstrip("/") + "/.well-known/openid-configuration")
        doc = await self._request("GET", url)
        if doc.get("issuer") != issuer:
            raise UpstreamError("issuer_mismatch")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            self._check_url(doc.get(name))
        self._discovery, self._discovery_at = doc, self._now()
        return doc

    async def _keys(self) -> list[dict[str, Any]]:
        if self._jwks is not None and self._now() - self._jwks_at < JWKS_TTL:
            return self._jwks
        doc = await self.discover()
        data = await self._request("GET", doc["jwks_uri"])
        keys = data.get("keys")
        if not isinstance(keys, list):
            raise UpstreamError("jwks_malformed")
        self._jwks = [k for k in keys if isinstance(k, dict)]
        self._jwks_at = self._now()
        return self._jwks

    async def _signing_key(self, header: dict[str, Any]) -> Any:
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in ALGORITHMS:
            raise UpstreamError("alg_not_allowed")
        if not isinstance(kid, str) or not kid:
            raise UpstreamError("kid_missing")
        match = [k for k in await self._keys() if k.get("kid") == kid]
        if len(match) != 1:
            raise UpstreamError("kid_unknown")
        jwk = match[0]
        if jwk.get("kty") != _KTY[alg]:
            raise UpstreamError("jwk_kty")
        if alg == "ES256" and jwk.get("crv") != "P-256":
            raise UpstreamError("jwk_curve")
        if jwk.get("alg") is not None and jwk.get("alg") != alg:
            raise UpstreamError("jwk_alg")
        if jwk.get("use") is not None and jwk.get("use") != "sig":
            raise UpstreamError("jwk_use")
        try:
            algo = RSAAlgorithm if alg == "RS256" else ECAlgorithm
            return algo.from_jwk(json.dumps(jwk))
        except (ValueError, TypeError, jwt.PyJWTError) as exc:
            raise UpstreamError("jwk_invalid") from exc

    # --- The authorization code flow ---------------------------------------------------------------------

    def new_pkce(self) -> tuple[str, str]:
        verifier = secrets.token_urlsafe(32)
        return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())

    async def authorization_url(self, state: str, nonce: str, challenge: str) -> str:
        doc = await self.discover()
        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        if len(self._config.hosted_domains) == 1:
            params["hd"] = self._config.hosted_domains[0]
        base = doc["authorization_endpoint"]
        return base + ("&" if "?" in base else "?") + urlencode(params)

    async def exchange(self, code: str, verifier: str) -> dict[str, Any]:
        doc = await self.discover()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._config.redirect_uri,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "code_verifier": verifier,
        }
        return await self._request("POST", doc["token_endpoint"], data=data)

    # --- ID token ----------------------------------------------------------------------------------------

    async def verify_id_token(self, id_token: str, nonce: str) -> VerifiedIdentity:
        cfg = self._config
        doc = await self.discover()
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise UpstreamError("token_malformed") from exc
        key = await self._signing_key(header)
        issuers = [doc["issuer"]]
        if doc["issuer"] == GOOGLE_ISSUER:
            issuers.append("accounts.google.com")
        try:
            claims = jwt.decode(
                id_token,
                key,
                algorithms=list(ALGORITHMS),
                audience=cfg.client_id,
                issuer=issuers,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise UpstreamError("token_invalid") from exc

        aud = claims.get("aud")
        azp = claims.get("azp")
        if azp is not None and azp != cfg.client_id:
            raise UpstreamError("azp_mismatch")
        if isinstance(aud, list) and len(aud) > 1 and azp is None:
            raise UpstreamError("azp_missing")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise UpstreamError("sub_missing")
        got_nonce = claims.get("nonce")
        if (
            not isinstance(got_nonce, str)
            or not nonce
            or not hmac.compare_digest(got_nonce.encode(), nonce.encode())
        ):
            raise UpstreamError("nonce_mismatch")

        email = claims.get("email")
        if not isinstance(email, str) or not email.isascii() or email.count("@") != 1:
            raise UpstreamError("email_invalid")
        local, _, domain = email.partition("@")
        if not local or not domain:
            raise UpstreamError("email_invalid")
        if claims.get("email_verified") is not True:
            raise UpstreamError("email_unverified")
        email = email.lower()
        domain = domain.lower()
        if domain not in {d.lower() for d in cfg.allowed_domains}:
            raise UpstreamError("domain_not_allowed")
        hd = claims.get("hd")
        if cfg.hosted_domains and (
            not isinstance(hd, str) or hd.lower() not in {d.lower() for d in cfg.hosted_domains}
        ):
            raise UpstreamError("hd_not_allowed")
        return VerifiedIdentity(email=email, sub=sub, hd=hd if isinstance(hd, str) else None)
