import json
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from knowledge_hub.directory.google import SCOPES, GoogleProvider, make
from knowledge_hub.directory.provider import GroupRefs, ProviderError
from tests.support.google_fake import make_google_transport

pytestmark = pytest.mark.unit

TOKEN_URI = "https://oauth2.googleapis.com/token"
SUBJECT = "reader@example.org"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def creds(tmp_path, key):
    def write(token_uri=TOKEN_URI):
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        path = tmp_path / "sa.json"
        path.write_text(json.dumps({"client_email": "sa@p.iam", "private_key": pem, "token_uri": token_uri}))
        return path

    return write


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def user(email, **kw):
    return {"primaryEmail": email, **kw}


def member(email, type_="USER", status="ACTIVE"):
    return {"email": email, "type": type_, "status": status}


def provider(creds, transport, clock=None):
    clock = clock or Clock()
    http = httpx.AsyncClient(transport=transport)
    return GoogleProvider(creds(), SUBJECT, http, now=clock.now, sleep=clock.sleep), clock


REFS = {"g": GroupRefs(("team@example.org",), ("lead@example.org",))}
USERS = [[user("a@example.org", aliases=["alias@example.org"]), user("b@example.org", suspended=True)]]


def groups(**extra):
    base = {
        "team@example.org": [[member("a@example.org"), member("x@other.com")]],
        "lead@example.org": [[member("a@example.org")]],
    }
    base.update(extra)
    return base


async def test_assertion_claims(creds, key):
    t = make_google_transport(groups(), USERS)
    p, clock = provider(creds, t)
    await p.fetch(REFS)
    tok = [r for r in t.requests if r.url.host == "oauth2.googleapis.com"][0]
    form = dict(x.split("=", 1) for x in tok.content.decode().split("&"))
    assert form["grant_type"].startswith("urn%3Aietf")
    from urllib.parse import unquote

    claims = jwt.decode(
        unquote(form["assertion"]), key.public_key(), algorithms=["RS256"], audience=TOKEN_URI,
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == "sa@p.iam"
    assert claims["sub"] == SUBJECT
    assert claims["scope"].split(" ") == list(SCOPES) and len(SCOPES) == 2
    assert claims["aud"] == TOKEN_URI


async def test_token_cached_then_refreshed(creds):
    t = make_google_transport(groups(), USERS)
    p, clock = provider(creds, t)
    n = lambda: len([r for r in t.requests if r.url.host == "oauth2.googleapis.com"])  # noqa: E731
    await p.fetch(REFS)
    await p.fetch(REFS)
    assert n() == 1
    clock.t += 3600 - 301
    await p.fetch(REFS)
    assert n() == 1
    clock.t += 3
    await p.fetch(REFS)
    assert n() == 2


async def test_members_users_and_rules(creds):
    g = groups(
        **{
            "team@example.org": [
                [member("a@example.org"), member("grp@example.org", "GROUP")],
                [
                    member("Alias@example.org"),
                    member("b@example.org"),
                    member("gone@example.org"),
                    member("x@other.com"),
                    member("s@other.com"),
                    member("unknown@other.com"),
                    member("c@example.org", status="SUSPENDED"),
                ],
            ]
        }
    )
    users = [[user("a@example.org", aliases=["alias@example.org"])], [user("b@example.org", suspended=True),
             user("c@example.org"),
             user("x@other.com"), user("s@other.com", suspended=True)], ]
    t = make_google_transport(g, users)
    p, _ = provider(creds, t)
    out = await p.fetch(REFS)
    got = {u.email: u.enabled for u in out["g"].members}
    assert got == {
        "a@example.org": True,
        "alias@example.org": True,
        "b@example.org": False,
        "gone@example.org": False,
        "x@other.com": True,
        "s@other.com": False,
        "unknown@other.com": False,
        "c@example.org": False,
    }
    assert all(u.email_verified for u in out["g"].members)
    assert [u.email for u in out["g"].approvers] == ["a@example.org"]
    assert len([r for r in t.requests if r.url.path == "/admin/directory/v1/users"]) == 2
    member_calls = [r for r in t.requests if r.url.path.endswith("/members")]
    assert member_calls and all(r.url.params["includeDerivedMembership"] == "true" for r in member_calls)


async def test_archived_user_not_enabled(creds):
    t = make_google_transport(groups(), [[user("a@example.org", archived=True)]])
    p, _ = provider(creds, t)
    out = await p.fetch(REFS)
    assert out["g"].members[0].enabled is False


async def test_customer_row_fails_whole_fetch(creds):
    rows = [[member("a@example.org"), {"type": "CUSTOMER", "id": "1"}]]
    t = make_google_transport(groups(**{"team@example.org": rows}), USERS)
    p, _ = provider(creds, t)
    with pytest.raises(ProviderError, match="group team@example.org contains the whole domain"):
        await p.fetch(REFS)


async def test_404(creds):
    p, _ = provider(creds, make_google_transport(groups(), USERS, fail={"not_found": "team@example.org"}))
    with pytest.raises(ProviderError, match="status 404"):
        await p.fetch(REFS)


async def test_429_retried(creds):
    p, clock = provider(creds, make_google_transport(groups(), USERS, fail={"429_once": True}))
    await p.fetch(REFS)
    assert clock.slept == [1]


async def test_persistent_500(creds):
    p, clock = provider(creds, make_google_transport(groups(), USERS, fail={"500_always": True}))
    with pytest.raises(ProviderError, match="status 500"):
        await p.fetch(REFS)
    assert clock.slept == [1, 3]


async def test_deadline_stops_retries(creds):
    p, clock = provider(creds, make_google_transport(groups(), USERS, fail={"500_always": True}))
    base = clock.now

    def now():
        clock.t += 30  # every reading of the clock costs 30 s
        return base()

    p._now = now
    with pytest.raises(ProviderError, match="status 500"):
        await p.fetch(REFS)
    assert clock.slept == [1]


@pytest.mark.parametrize("fail", ["500_members_page2", "500_users_page2", "malformed_members"])
async def test_page_failures_never_partial(creds, fail):
    g = groups(**{"team@example.org": [[member("a@example.org")], [member("b@example.org")]]})
    users = [[user("a@example.org")], [user("b@example.org")]]
    p, _ = provider(creds, make_google_transport(g, users, fail={fail: True}))
    with pytest.raises(ProviderError):
        await p.fetch(REFS)


async def test_wrong_token_uri_refused(creds):
    with pytest.raises(ProviderError, match="token_uri"):
        GoogleProvider(creds("https://evil.example/token"), SUBJECT, None)


def test_missing_settings():
    text = "google: KH_DIRECTORY_GOOGLE_CREDENTIALS_FILE and KH_DIRECTORY_GOOGLE_SUBJECT are required"
    with pytest.raises(ProviderError) as ei:
        GoogleProvider(None, SUBJECT, None)
    assert str(ei.value) == text
    with pytest.raises(ProviderError, match="are required"):
        make(SimpleNamespace(directory_google_credentials_file="x", directory_google_subject=None), None)


async def test_error_has_no_body_and_scopes_are_two(creds):
    t = make_google_transport(groups(), USERS, fail={"500_always": True})
    p, _ = provider(creds, t)
    with pytest.raises(ProviderError) as ei:
        await p.fetch(REFS)
    assert "secret-body" not in str(ei.value)
    for r in t.requests:
        assert "scope" not in r.url.params
    assert set(SCOPES) == {
        "https://www.googleapis.com/auth/admin.directory.group.member.readonly",
        "https://www.googleapis.com/auth/admin.directory.user.readonly",
    }
