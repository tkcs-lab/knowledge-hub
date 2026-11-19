"""Opaque token and secret helpers; only the SHA-256 of a token is ever stored."""

from __future__ import annotations

import base64
import hashlib
import secrets

ACCESS_PREFIX = "kha_"
REFRESH_PREFIX = "khr_"


def new_secret() -> str:
    """Return 43 characters of unpadded urlsafe base64 of 32 random bytes (states, codes, tickets, csrf)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def new_token(prefix: str) -> str:
    return prefix + new_secret()


def token_hash(token: str) -> bytes:
    """Return the SHA-256 digest of the ASCII token string."""
    return hashlib.sha256(token.encode("ascii")).digest()
