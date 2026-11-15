"""Opaque search cursors bound to caller, audiences and query (plan 3.5)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterable

from knowledge_hub.mcp.errors import CURSOR_FOREIGN, KhError

DEFAULT_MAX_OFFSET = 100  # KH_SEARCH_MAX_OFFSET default
_MAC_LEN = 32


def _mac(key: bytes, sub: str, audiences: Iterable[str], query: str, offset: int) -> bytes:
    aud = hashlib.sha256("\n".join(sorted(set(audiences))).encode()).hexdigest()
    qh = hashlib.sha256(query.encode()).hexdigest()
    msg = f"{sub}|{aud}|{qh}|{offset}".encode()
    return hmac.new(key, msg, hashlib.sha256).digest()


def _foreign() -> KhError:
    return KhError("CURSOR_FOREIGN", CURSOR_FOREIGN)


def encode_cursor(key: bytes, sub: str, audiences: Iterable[str], query: str, offset: int) -> str:
    off = offset.to_bytes(8, "big")
    token = _mac(key, sub, audiences, query, offset) + off
    return base64.urlsafe_b64encode(token).decode().rstrip("=")


def decode_cursor(
    key: bytes,
    sub: str,
    audiences: Iterable[str],
    query: str,
    token: str,
    *,
    max_offset: int = DEFAULT_MAX_OFFSET,
) -> int:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        raise _foreign() from None
    if len(raw) != _MAC_LEN + 8:
        raise _foreign()
    offset = int.from_bytes(raw[_MAC_LEN:], "big")
    if not hmac.compare_digest(raw[:_MAC_LEN], _mac(key, sub, audiences, query, offset)):
        raise _foreign()
    if offset > max_offset:
        raise _foreign()
    return offset
