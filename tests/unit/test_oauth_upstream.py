"""The upstream OIDC client: one test per refusal and one per success, against an in-process fake IdP."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from starlette.applications import Starlette

from knowledge_hub.oauth.upstream import (
    UpstreamClient,
    UpstreamConfig,
    UpstreamError,
    VerifiedIdentity,
)
from tests.support.fake_idp import FIXED_CODE, forge, make_fake_idp

pytestmark = pytest.mark.unit

ISSUER = "https://idp.example.test"
CLIENT_ID = "kh-client"
SECRET = "s3cret"
REDIRECT = "https://hub.example.test/oauth/callback"
NONCE = "nonce-1"


@dataclass
class Env:
    app: Starlette
    idp: Any
    client: UpstreamClient
    clock: list[float]
    http: httpx.AsyncClient

    def token(
        self, drop: tuple[str, ...] = (), alg: str = "RS256", kid: str | None = None, **over: Any
    ) -> str:
        t = time.time()
        claims: dict[str, Any] = {
            **self.idp.claims,
            "iat": int(t) - 10,
            "exp": int(t) + 300,
            "nonce": NONCE,
            **over,
        }
        for name in drop:
            claims.pop(name, None)
        return self.idp.sign(claims, alg=alg, kid=kid)

    async def verify(self, token: str, nonce: str = NONCE) -> VerifiedIdentity:
        return await self.client.verify_id_token(token, nonce)

    async def refused(self, token: str, code: str, nonce: str = NONCE) -> None:
        with pytest.raises(UpstreamError) as err:
            await self.verify(token, nonce)
        assert err.value.code == code


def _build(
    issuer: str = ISSUER,
    *,
    allow_http: bool = False,
    hosted: tuple[str, ...] = (),
    allowed: tuple[str, ...] = ("example.test",),
) -> tuple[Starlette, UpstreamConfig]:
    app = make_fake_idp(issuer, CLIENT_ID, SECRET)
    cfg = UpstreamConfig(issuer, CLIENT_ID, SECRET, allowed, hosted, REDIRECT, allow_http)
    return app, cfg


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    app, cfg = _build()
    clock = [1_000_000.0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        yield Env(app, app.state.idp, UpstreamClient(cfg, http, now=lambda: clock[0]), clock, http)


@pytest.fixture
async def hosted_env() -> AsyncIterator[Env]:
    app, cfg = _build(hosted=("example.test",))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        yield Env(app, app.state.idp, UpstreamClient(cfg, http), [0.0], http)


# --- successes -------------------------------------------------------------------------------------------


async def test_valid_token(env: Env) -> None:
    assert await env.verify(env.token()) == VerifiedIdentity("alice@example.test", "sub-1", "example.test")


async def test_email_is_lower_cased(env: Env) -> None:
    ident = await env.verify(env.token(email="Alice@Example.Test"))
    assert ident.email == "alice@example.test"


async def test_es256_accepted(env: Env) -> None:
    assert (await env.verify(env.token(alg="ES256"))).email == "alice@example.test"


async def test_google_issuer_accepts_bare_accounts_google_com() -> None:
    issuer = "https://accounts.google.com"
    app, cfg = _build(issuer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        e = Env(app, app.state.idp, UpstreamClient(cfg, http), [0.0], http)
        assert (await e.verify(e.token(iss="accounts.google.com"))).sub == "sub-1"
        assert (await e.verify(e.token(iss=issuer))).sub == "sub-1"


async def test_bare_google_issuer_refused_for_other_issuer(env: Env) -> None:
    await env.refused(env.token(iss="accounts.google.com"), "token_invalid")


async def test_multiple_audiences_with_matching_azp(env: Env) -> None:
    ident = await env.verify(env.token(aud=[CLIENT_ID, "other"], azp=CLIENT_ID))
    assert ident.sub == "sub-1"


async def test_hosted_domain_accepted(hosted_env: Env) -> None:
    assert (await hosted_env.verify(hosted_env.token())).hd == "example.test"


async def test_unknown_kid_after_rotation_refetches_once(env: Env) -> None:
    await env.verify(env.token())
    assert env.idp.hits["jwks"] == 1
    env.idp.add_key("rsa-2")
    env.clock[0] += 301
    assert (await env.verify(env.token(kid="rsa-2"))).sub == "sub-1"
    assert env.idp.hits["jwks"] == 2


# --- refusals --------------------------------------------------------------------------------------------


async def test_wrong_issuer(env: Env) -> None:
    await env.refused(env.token(iss="https://evil.example.test"), "token_invalid")


async def test_wrong_audience(env: Env) -> None:
    await env.refused(env.token(aud="someone-else"), "token_invalid")


async def test_several_audiences_wrong_azp(env: Env) -> None:
    await env.refused(env.token(aud=[CLIENT_ID, "other"], azp="other"), "azp_mismatch")


async def test_several_audiences_no_azp(env: Env) -> None:
    await env.refused(env.token(aud=[CLIENT_ID, "other"]), "azp_missing")


async def test_single_audience_with_different_azp(env: Env) -> None:
    await env.refused(env.token(azp="other"), "azp_mismatch")


async def test_alg_none(env: Env) -> None:
    t = int(time.time())
    claims = {**env.idp.claims, "iat": t, "exp": t + 300, "nonce": NONCE}
    await env.refused(forge({"alg": "none", "typ": "JWT", "kid": "rsa-1"}, claims), "alg_not_allowed")


async def test_hs256_signed_with_public_key_bytes(env: Env) -> None:
    t = int(time.time())
    claims = {**env.idp.claims, "iat": t, "exp": t + 300, "nonce": NONCE}
    token = forge({"alg": "HS256", "typ": "JWT", "kid": "rsa-1"}, claims, env.idp.public_pem())
    await env.refused(token, "alg_not_allowed")


@pytest.mark.parametrize("alg", ["RS384", "PS256"])
async def test_other_algorithms_refused(env: Env, alg: str) -> None:
    await env.refused(env.token(alg=alg), "alg_not_allowed")


async def test_jwk_kty_not_fitting_alg(env: Env) -> None:
    # an ES256 header pointing at the RSA key's kid
    await env.refused(env.token(alg="ES256", kid="rsa-1"), "jwk_kty")
    # an RS256 header pointing at the EC key's kid
    await env.refused(env.token(alg="RS256", kid="ec-1"), "jwk_kty")


async def test_jwk_alg_or_use_mismatch(env: Env) -> None:
    env.idp.jwk_overrides["rsa-1"] = {"alg": "RS512"}
    await env.refused(env.token(), "jwk_alg")
    env.clock[0] += 301
    env.idp.jwk_overrides["rsa-1"] = {"use": "enc"}
    await env.refused(env.token(), "jwk_use")


async def test_expired(env: Env) -> None:
    await env.refused(env.token(exp=int(time.time()) - 120), "token_invalid")


@pytest.mark.parametrize("claim", ["exp", "iat", "sub"])
async def test_required_claim_missing(env: Env, claim: str) -> None:
    await env.refused(env.token(drop=(claim,)), "token_invalid")


async def test_sub_empty(env: Env) -> None:
    await env.refused(env.token(sub=""), "sub_missing")


async def test_email_missing(env: Env) -> None:
    await env.refused(env.token(drop=("email",)), "email_invalid")


async def test_email_verified_missing(env: Env) -> None:
    await env.refused(env.token(drop=("email_verified",)), "email_unverified")


@pytest.mark.parametrize("value", ["true", False, 1])
async def test_email_verified_not_boolean_true(env: Env, value: object) -> None:
    await env.refused(env.token(email_verified=value), "email_unverified")


@pytest.mark.parametrize("email", ["alicé@example.test", "a@b@example.test", "@example.test", "alice@"])
async def test_bad_email_shape(env: Env, email: str) -> None:
    await env.refused(env.token(email=email), "email_invalid")


async def test_email_domain_not_allowed(env: Env) -> None:
    await env.refused(env.token(email="alice@other.test"), "domain_not_allowed")


async def test_hd_different(hosted_env: Env) -> None:
    await hosted_env.refused(hosted_env.token(hd="other.test"), "hd_not_allowed")


async def test_hd_missing_while_hosted_domains_set(hosted_env: Env) -> None:
    await hosted_env.refused(hosted_env.token(drop=("hd",)), "hd_not_allowed")


async def test_nonce_missing(env: Env) -> None:
    await env.refused(env.token(drop=("nonce",)), "nonce_mismatch")


async def test_nonce_mismatch(env: Env) -> None:
    await env.refused(env.token(nonce="other"), "nonce_mismatch")


async def test_unknown_kid_refetches_once_then_not_within_five_minutes(env: Env) -> None:
    await env.verify(env.token())
    assert env.idp.hits["jwks"] == 1
    env.clock[0] += 301
    await env.refused(env.token(kid="ghost"), "kid_unknown")
    assert env.idp.hits["jwks"] == 2
    env.clock[0] += 1
    await env.refused(env.token(kid="ghost"), "kid_unknown")
    assert env.idp.hits["jwks"] == 2


async def test_kid_missing(env: Env) -> None:
    t = int(time.time())
    claims = {**env.idp.claims, "iat": t, "exp": t + 300, "nonce": NONCE}
    token = jwt.encode(claims, env.idp.keys["rsa-1"], algorithm="RS256")
    await env.refused(token, "kid_missing")


async def test_tampered_signature(env: Env) -> None:
    head, body, sig = env.token().split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    await env.refused(f"{head}.{body}.{flipped}", "token_invalid")


async def test_garbage_token(env: Env) -> None:
    await env.refused("not-a-jwt", "token_malformed")


# --- discovery and transport -----------------------------------------------------------------------------


async def test_discovery_issuer_differs(env: Env) -> None:
    env.idp.discovery["issuer"] = "https://other.example.test"
    with pytest.raises(UpstreamError) as err:
        await env.client.discover()
    assert err.value.code == "issuer_mismatch"


async def test_discovered_endpoint_on_another_host(env: Env) -> None:
    env.idp.discovery["token_endpoint"] = "https://evil.example.test/token"
    with pytest.raises(UpstreamError) as err:
        await env.client.discover()
    assert err.value.code == "endpoint_host"


async def test_http_endpoint_refused_unless_allowed(env: Env) -> None:
    env.idp.discovery["jwks_uri"] = f"{ISSUER.replace('https', 'http')}/jwks"
    with pytest.raises(UpstreamError) as err:
        await env.client.discover()
    assert err.value.code == "endpoint_scheme"

    app, cfg = _build("http://idp.example.test", allow_http=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        e = Env(app, app.state.idp, UpstreamClient(cfg, http), [0.0], http)
        assert (await e.verify(e.token())).sub == "sub-1"


async def test_http_issuer_refused_without_allow_http() -> None:
    app, cfg = _build("http://idp.example.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        with pytest.raises(UpstreamError) as err:
            await UpstreamClient(cfg, http).discover()
    assert err.value.code == "endpoint_scheme"


@pytest.mark.parametrize("which", ["discovery", "jwks"])
async def test_response_over_one_mib_refused(env: Env, which: str) -> None:
    env.idp.pad[which] = 1024 * 1024
    with pytest.raises(UpstreamError) as err:
        await env.verify(env.token())
    assert err.value.code == "response_too_large"


async def test_token_response_over_one_mib_refused(env: Env) -> None:
    env.idp.pad["token"] = 1024 * 1024
    verifier, challenge = env.client.new_pkce()
    await _authorize(env, challenge)
    with pytest.raises(UpstreamError) as err:
        await env.client.exchange(FIXED_CODE, verifier)
    assert err.value.code == "response_too_large"


async def test_redirect_not_followed(env: Env) -> None:
    env.idp.discovery["jwks_uri"] = f"{ISSUER}/redirect"
    with pytest.raises(UpstreamError) as err:
        await env.verify(env.token())
    assert err.value.code == "upstream_status"


async def test_discovery_cached_for_one_hour(env: Env) -> None:
    await env.client.discover()
    env.clock[0] += 3599
    await env.client.discover()
    assert env.idp.hits["discovery"] == 1
    env.clock[0] += 2
    await env.client.discover()
    assert env.idp.hits["discovery"] == 2


# --- the code flow ---------------------------------------------------------------------------------------


async def _authorize(env: Env, challenge: str) -> None:
    url = await env.client.authorization_url("st", NONCE, challenge)
    response = await env.http.get(url)
    assert response.status_code == 302


async def test_authorization_url_parameters(env: Env) -> None:
    verifier, challenge = env.client.new_pkce()
    assert len(verifier) == 43
    url = await env.client.authorization_url("state-1", "nonce-9", challenge)
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ISSUER}/authorize"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q["response_type"] == "code"
    assert q["scope"] == "openid email"
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == challenge
    got = (q["state"], q["nonce"], q["client_id"], q["redirect_uri"])
    assert got == ("state-1", "nonce-9", CLIENT_ID, REDIRECT)
    assert "hd" not in q


async def test_authorization_url_carries_single_hosted_domain(hosted_env: Env) -> None:
    url = await hosted_env.client.authorization_url("s", "n", "c")
    assert parse_qs(urlsplit(url).query)["hd"] == ["example.test"]


async def test_exchange_then_verify(env: Env) -> None:
    verifier, challenge = env.client.new_pkce()
    await _authorize(env, challenge)
    tokens = await env.client.exchange(FIXED_CODE, verifier)
    # the fake takes the nonce from the authorize request, which used NONCE
    assert (await env.verify(tokens["id_token"])).email == "alice@example.test"


async def test_exchange_with_wrong_verifier_fails(env: Env) -> None:
    _, challenge = env.client.new_pkce()
    await _authorize(env, challenge)
    with pytest.raises(UpstreamError) as err:
        await env.client.exchange(FIXED_CODE, "x" * 43)
    assert err.value.code == "upstream_status"
