"""Signed, purpose-bound, expiring tokens for the panel cookies."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    if not text.isascii():
        raise ValueError("not ascii")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, key: bytes) -> None:
        self._key = key

    def _mac(self, purpose: str, body: str) -> str:
        subkey = hmac.new(self._key, b"kh-panel/" + purpose.encode(), hashlib.sha256).digest()
        return _b64(hmac.new(subkey, body.encode("ascii"), hashlib.sha256).digest())

    def sign(self, purpose: str, payload: dict[str, Any], ttl_seconds: int, now: float) -> str:
        data = {**payload, "exp": int(now) + int(ttl_seconds)}
        body = _b64(json.dumps(data, separators=(",", ":"), sort_keys=True).encode())
        return f"{body}.{self._mac(purpose, body)}"

    def verify(self, purpose: str, token: str, now: float) -> dict[str, Any] | None:
        try:
            parts = token.split(".")
            if len(parts) != 2:
                return None
            body, sig = parts
            if not hmac.compare_digest(self._mac(purpose, body), sig):
                return None
            data = json.loads(_unb64(body))
            if not isinstance(data, dict):
                return None
            exp = data.get("exp")
            if isinstance(exp, bool) or not isinstance(exp, (int, float)) or now >= exp:
                return None
            return data
        except (ValueError, TypeError, UnicodeError, binascii.Error, AttributeError):
            return None
