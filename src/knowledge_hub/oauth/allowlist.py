"""The redirect-URI allowlist (``KH_OAUTH_REDIRECT_ALLOWLIST``)."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _loopback_entry(entry: str) -> str | None:
    """Return the host of a loopback entry (``http://localhost``, ``http://127.0.0.1``), else ``None``."""
    try:
        parts = urlsplit(entry)
        if (
            parts.scheme == "http"
            and parts.hostname in LOOPBACK_HOSTS
            and parts.netloc == parts.hostname
            and parts.path in ("", "/")
            and not parts.query
            and not parts.fragment
        ):
            return parts.hostname
    except ValueError:
        return None
    return None


def redirect_allowed(uri: str, allowlist: Sequence[str]) -> bool:
    """Return whether ``uri`` matches an entry of ``allowlist``.

    An ``https`` entry matches only the identical URI, which must carry no userinfo, query or fragment.
    A loopback entry matches a URI of scheme ``http`` whose ``hostname`` is exactly the entry's host, with
    no userinfo and no fragment, on any port and path. Anything unparsable is refused.
    """
    try:
        parts = urlsplit(uri)
        _ = parts.port  # raises ValueError on a bad port
        host = parts.hostname
    except ValueError:
        return False
    if not parts.scheme or "@" in parts.netloc or parts.fragment or any(c.isspace() for c in uri):
        return False
    for entry in allowlist:
        entry = entry.strip()
        if not entry:
            continue
        loop = _loopback_entry(entry)
        if loop is not None:
            if parts.scheme == "http" and host == loop:
                return True
            continue
        if uri == entry and not parts.query:
            return True
    return False
