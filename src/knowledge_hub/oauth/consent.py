"""The consent page and its response headers (plan 3.9 item 4). Every shown value is HTML-escaped."""

from __future__ import annotations

from html import escape

TITLE = "Knowledge hub: allow access"


def render_consent(client_name: str, redirect_host: str, email: str, csrf: str) -> str:
    name, host, mail, token = (escape(v, quote=True) for v in (client_name, redirect_host, email, csrf))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{TITLE}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem; }}
button {{ font-size: 1rem; padding: .5rem 1.25rem; margin-right: .5rem; }}
</style>
</head>
<body>
<h1>{TITLE}</h1>
<p><strong>{name}</strong> wants to access the knowledge hub as <strong>{mail}</strong>.</p>
<p>After your decision you return to <strong>{host}</strong>.</p>
<form method="post" action="/oauth/consent">
<input type="hidden" name="csrf" value="{token}">
<button type="submit" name="decision" value="allow">Allow</button>
<button type="submit" name="decision" value="deny">Deny</button>
</form>
</body>
</html>
"""


def consent_headers(redirect_origin: str) -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'unsafe-inline'; "
            f"form-action 'self' {redirect_origin}; frame-ancestors 'none'"
        ),
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }
