"""Opaque search cursors bound to caller, audiences and query (plan 3.5)."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
from collections.abc import Iterable

from knowledge_hub.mcp.errors import CURSOR_FOREIGN, KhError

DEFAULT_MAX_OFFSET = 100  # KH_SEARCH_MAX_OFFSET default
_MAC_LEN = 32
_DATE_LEN = 4


def _mac(
    key: bytes, sub: str, audiences: Iterable[str], query: str, offset: int, as_of: dt.date
) -> bytes:
    aud = hashlib.sha256("\n".join(sorted(set(audiences))).encode()).hexdigest()
    qh = hashlib.sha256(query.encode()).hexdigest()
    msg = f"{sub}|{aud}|{qh}|{offset}|{as_of.isoformat()}".encode()
    return hmac.new(key, msg, hashlib.sha256).digest()


def _foreign() -> KhError:
    return KhError("CURSOR_FOREIGN", CURSOR_FOREIGN)


def encode_cursor(
    key: bytes, sub: str, audiences: Iterable[str], query: str, offset: int, as_of: dt.date
) -> str:
    off = offset.to_bytes(8, "big")
    day = as_of.toordinal().to_bytes(_DATE_LEN, "big")
    token = _mac(key, sub, audiences, query, offset, as_of) + off + day
    return base64.urlsafe_b64encode(token).decode().rstrip("=")


def decode_cursor(
    key: bytes,
    sub: str,
    audiences: Iterable[str],
    query: str,
    token: str,
    *,
    max_offset: int = DEFAULT_MAX_OFFSET,
) -> tuple[int, dt.date]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        raise _foreign() from None
    if len(raw) != _MAC_LEN + 8 + _DATE_LEN:
        raise _foreign()
    offset = int.from_bytes(raw[_MAC_LEN : _MAC_LEN + 8], "big")
    try:
        as_of = dt.date.fromordinal(int.from_bytes(raw[_MAC_LEN + 8 :], "big"))
    except (ValueError, OverflowError):
        raise _foreign() from None
    if not hmac.compare_digest(raw[:_MAC_LEN], _mac(key, sub, audiences, query, offset, as_of)):
        raise _foreign()
    if offset > max_offset:
        raise _foreign()
    return offset, as_of
