"""Forgejo webhook signature check (``X-Forgejo-Signature``: hex HMAC-SHA256 of the raw body)."""

from __future__ import annotations

import hashlib
import hmac
import re

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def verify_forgejo_signature(body: bytes, header: str | None, secret: bytes | str) -> bool:
    """True only when ``header`` is the hex HMAC-SHA256 of ``body`` under ``secret``."""
    if not header or not secret:
        return False
    value = header.strip()
    if not _HEX64.fullmatch(value):
        return False
    key = secret.encode() if isinstance(secret, str) else secret
    expected = hmac.new(key, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, value.lower())
