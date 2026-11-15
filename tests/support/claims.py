"""Signed PrivacyFence test claims. The key is generated per process; nothing here is a secret."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from knowledge_hub.auth.pf_claim_verify import CLAIM_TYPE, ClaimVerifier, request_hash

ISSUER = "https://pf.test.example"
AUDIENCE = "https://hub.test.example/mcp"

_KEY = Ed25519PrivateKey.generate()
_RAW_PUBLIC = _KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


KID = _b64url(hashlib.sha256(_RAW_PUBLIC).digest())[:16]


def static_jwks() -> dict[str, Any]:
    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": _b64url(_RAW_PUBLIC),
                "use": "sig",
                "alg": "EdDSA",
                "kid": KID,
            }
        ]
    }


def mint_claim(
    sub: str,
    principal_type: str,
    audiences: list[str] | tuple[str, ...] = (),
    email: str | None = None,
    tool: str | None = None,
    arguments: dict[str, Any] | None = None,
    lifetime: float = 60,
    now: float | None = None,
    **overrides: Any,
) -> str:
    """A claim for one request. With ``tool`` it is bound to that call through ``req``.

    An override of ``None`` removes the key from the payload.
    """
    issued = time.time() if now is None else now
    payload: dict[str, Any] = {
        "claim_version": 1,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": sub,
        "principal_type": principal_type,
        "audiences": sorted(audiences),
        "tools": [],
        "iat": issued,
        "exp": issued + lifetime,
        "jti": str(uuid.uuid4()),
    }
    if email is not None:
        payload["email"] = email
    if principal_type == "n8n_workflow":
        payload["token_id"] = sub.removeprefix("n8n-token:")
        payload["approved_by"] = "approver"
    if tool is not None:
        payload["req"] = request_hash(tool, arguments or {})
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return jwt.encode(payload, _KEY, algorithm="EdDSA", headers={"kid": KID, "typ": CLAIM_TYPE})


def verifier_for_tests(**kwargs: Any) -> ClaimVerifier:
    """A verifier over the static JWKS. Replay is left to the middleware's cache unless overridden."""
    kwargs.setdefault("replay_cache", None)
    return ClaimVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=static_jwks(), **kwargs)


def rpc_body(method: str, params: dict[str, Any] | None = None, id: int = 1) -> bytes:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode("utf-8")


def call_body(tool: str, arguments: dict[str, Any] | None = None) -> bytes:
    params: dict[str, Any] = {"name": tool}
    if arguments is not None:
        params["arguments"] = arguments
    return rpc_body("tools/call", params)
