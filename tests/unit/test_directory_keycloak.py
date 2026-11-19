from urllib.parse import parse_qs, unquote

import httpx
import pytest

from knowledge_hub.directory.keycloak import KeycloakProvider
from knowledge_hub.directory.provider import GroupRefs, ProviderError
from knowledge_hub.directory.rules import effective
from tests.support import keycloak_expected as exp

pytestmark = pytest.mark.unit

BASE = "https://kc.example.org"
ADMIN = "/admin/realms/r"


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def u(uid, email="", enabled=True, verified=True, **kw):
    row = {"id": uid, "username": uid, "enabled": enabled, "emailVerified": verified, **kw}
    if email is not None:
        row["email"] = email
    return row


class Fake:
    """groups: id -> {"path", "members", "children"}; failures by (path-suffix) -> list of statuses."""

    def __init__(self, groups, fail=None, page=100):
        self.groups = groups
        self.fail = {k: list(v) for k, v in (fail or {}).items()}
        self.requests = []
        self.page = page

    def _page(self, rows, q):
        first, mx = int(q["first"]), int(q["max"])
        return rows[first : first + mx]

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        q = dict(request.url.params)
        if path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        assert request.headers["authorization"] == "Bearer tok"
        for suffix, statuses in self.fail.items():
            if (path + "?first=" + q.get("first", "")).endswith(suffix) or path.endswith(suffix):
                if statuses:
                    status = statuses.pop(0)
                    if status != 200:
                        return httpx.Response(status)
        if path == f"{ADMIN}/groups":
            hits = [
                {"id": gid, "path": g["path"]} for gid, g in self.groups.items() if q["search"] in g["path"]
            ]
            return httpx.Response(200, json=hits)
        if path.startswith(f"{ADMIN}/group-by-path/"):
            wanted = "/" + unquote(path[len(f"{ADMIN}/group-by-path/") :])
            for gid, g in self.groups.items():
                if g["path"] == wanted:
                    return httpx.Response(200, json={"id": gid, "path": wanted})
            return httpx.Response(404)
        gid = path.split("/")[-2]
        kind = path.split("/")[-1]
        if gid not in self.groups:
            return httpx.Response(404)
        if kind == "members":
            assert q["briefRepresentation"] == "false"
            return httpx.Response(200, json=self._page(self.groups[gid]["members"], q))
        if kind == "children":
            kids = [{"id": c} for c in self.groups[gid].get("children", [])]
            return httpx.Response(200, json=self._page(kids, q))
        return httpx.Response(404)


def provider(fake, *, subgroups=False, url=BASE, env="test", clock=None):
    clock = clock or Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return KeycloakProvider(
        url, "r", "cid", "sec", subgroups, http, sleep=clock.sleep, now=clock.now, env=env
    ), clock


def refs(m=("all",), a=()):
    return {"g": GroupRefs(tuple(m), tuple(a))}


def simple(members=None, **kw):
    return {"id1": {"path": "/all", "members": members if members is not None else [u("a", "A@X.org")]}, **kw}


async def test_token_request_is_client_credentials():
    fake = Fake(simple())
    p, _ = provider(fake)
    await p.fetch(refs())
    token = fake.requests[0]
    assert token.method == "POST" and token.url.path == "/realms/r/protocol/openid-connect/token"
    form = parse_qs(token.content.decode())
    assert form["grant_type"] == ["client_credentials"] and form["client_id"] == ["cid"]
    assert sum(1 for r in fake.requests if "openid-connect" in r.url.path) == 1


async def test_top_level_keeps_only_the_exact_path():
    groups = {
        "x": {"path": "/other/all", "members": [u("wrong", "w@x.org")]},
        "id1": {"path": "/all", "members": [u("a", "a@x.org")]},
    }
    p, _ = provider(Fake(groups))
    got = await p.fetch(refs())
    assert [m.email for m in got["g"].members] == ["a@x.org"]


async def test_path_reference_uses_group_by_path_with_encoded_segments():
    fake = Fake({"id1": {"path": "/a b/c-d", "members": [u("a", "a@x.org")]}})
    p, _ = provider(fake)
    got = await p.fetch(refs(["/a b/c-d"]))
    assert [m.email for m in got["g"].members] == ["a@x.org"]
    assert any(r.url.raw_path.startswith(b"/admin/realms/r/group-by-path/a%20b/c-d") for r in fake.requests)


@pytest.mark.parametrize("ref", ["/a/../b", "/a/", "/a/b/", "a/b", "/a.b", "", "-a", "a;b", "/", "//a"])
async def test_bad_references_are_refused_without_request(ref):
    fake = Fake(simple())
    p, _ = provider(fake)
    with pytest.raises(ProviderError):
        await p.fetch(refs([ref]))
    assert fake.requests == []


async def test_two_member_pages():
    members = [u(f"u{i}", f"u{i}@x.org") for i in range(150)]
    fake = Fake(simple(members))
    p, _ = provider(fake)
    got = await p.fetch(refs())
    assert len(got["g"].members) == 150
    pages = [r for r in fake.requests if r.url.path.endswith("/members")]
    assert [r.url.params["first"] for r in pages] == ["0", "100"]


async def test_direct_members_only_by_default():
    groups = simple()
    groups["id1"]["children"] = ["c"]
    groups["c"] = {"path": "/all/c", "members": [u("kid", "kid@x.org")]}
    p, _ = provider(Fake(groups))
    got = await p.fetch(refs())
    assert [m.email for m in got["g"].members] == ["a@x.org"]


async def test_subgroups_included_cycle_safe():
    groups = {
        "id1": {"path": "/all", "members": [u("a", "a@x.org")], "children": ["c"]},
        "c": {"path": "/all/c", "members": [u("kid", "kid@x.org"), u("a", "a@x.org")], "children": ["id1"]},
    }
    p, _ = provider(Fake(groups), subgroups=True)
    got = await p.fetch(refs())
    assert sorted(m.email for m in got["g"].members) == ["a@x.org", "kid@x.org"]


def chain(n):
    groups = {}
    for i in range(n + 1):
        groups[f"id{i}" if i else "id1"] = {
            "path": "/all" if i == 0 else f"/all/{i}",
            "members": [],
            "children": [f"id{i + 1}"] if i < n else [],
        }
    # id0 would clash with the root lookup; rebuild with the root as id1
    out = {"id1": {"path": "/all", "members": [], "children": ["n1"]}}
    for i in range(1, n + 1):
        out[f"n{i}"] = {"path": f"/all/{i}", "members": [], "children": [f"n{i + 1}"] if i < n else []}
    return out


async def test_depth_ten_is_fine_and_eleven_raises():
    p, _ = provider(Fake(chain(10)), subgroups=True)
    await p.fetch(refs())
    p, _ = provider(Fake(chain(11)), subgroups=True)
    with pytest.raises(ProviderError, match="too deep"):
        await p.fetch(refs())


async def test_flags_carry_through_and_missing_flag_is_false():
    rows = [
        u("a", "a@x.org", enabled=False, verified=True),
        {"id": "b", "username": "b", "email": "b@x.org"},
        {"id": "c", "username": "c", "email": "c@x.org", "enabled": "true", "emailVerified": 1},
    ]
    p, _ = provider(Fake(simple(rows)))
    got = {m.username: m for m in (await p.fetch(refs()))["g"].members}
    assert (got["a"].enabled, got["a"].email_verified) == (False, True)
    assert (got["b"].enabled, got["b"].email_verified) == (False, False)
    assert (got["c"].enabled, got["c"].email_verified) == (False, False)


async def test_user_without_email_has_empty_email():
    p, _ = provider(Fake(simple([u("a", None)])))
    got = await p.fetch(refs())
    assert got["g"].members[0].email == ""


async def test_empty_group_is_valid():
    p, _ = provider(Fake(simple([])))
    assert (await p.fetch(refs()))["g"].members == ()


async def test_403_raises_status_only():
    fake = Fake(simple(), fail={"/groups": [403]})
    p, _ = provider(fake)
    with pytest.raises(ProviderError) as err:
        await p.fetch(refs())
    assert str(err.value) == "keycloak groups: status 403"


async def test_missing_group_raises():
    p, _ = provider(Fake(simple()))
    with pytest.raises(ProviderError, match="not found"):
        await p.fetch(refs(["nope"]))
    with pytest.raises(ProviderError, match="not found"):
        await p.fetch(refs(["/nope"]))


async def test_5xx_on_page_two_is_never_partial():
    members = [u(f"u{i}", f"u{i}@x.org") for i in range(150)]
    fake = Fake(simple(members), fail={"/members?first=100": [500, 500, 500, 500]})
    p, clock = provider(fake)
    with pytest.raises(ProviderError, match="status 500"):
        await p.fetch(refs())
    assert clock.slept == [1, 3]


async def test_retry_recovers_after_one_429():
    fake = Fake(simple(), fail={"/groups": [429, 200]})
    p, clock = provider(fake)
    got = await p.fetch(refs())
    assert len(got["g"].members) == 1 and clock.slept == [1]


@pytest.mark.parametrize("body", ["not a list", [1, 2], {"a": 1}])
async def test_malformed_page_raises(body):
    fake = Fake(simple())
    base = fake.handler

    def handler(request):
        if request.url.path.endswith("/members"):
            return httpx.Response(200, json=body)
        return base(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = KeycloakProvider(BASE, "r", "cid", "sec", False, http)
    with pytest.raises(ProviderError, match="malformed"):
        await p.fetch(refs())


async def test_persistent_5xx_sleeps_one_then_three():
    fake = Fake(simple(), fail={"/groups": [503, 503, 503, 503]})
    p, clock = provider(fake)
    with pytest.raises(ProviderError, match="status 503"):
        await p.fetch(refs())
    assert clock.slept == [1, 3]


def test_plain_http_with_dotted_host_is_refused():
    with pytest.raises(ProviderError, match="must be https"):
        KeycloakProvider("http://keycloak.example.com", "r", "c", "s", False, env="prod")


def test_plain_http_with_single_label_host_is_accepted():
    KeycloakProvider("http://keycloak:8080", "r", "c", "s", False, env="prod")


def test_plain_http_is_accepted_in_dev_and_test():
    KeycloakProvider("http://localhost.example.com", "r", "c", "s", False, env="dev")
    KeycloakProvider("http://kc.example.com", "r", "c", "s", False, env="test")


def test_missing_setting_raises():
    with pytest.raises(ProviderError, match="are required"):
        KeycloakProvider("", "r", "c", "s", False)


def realm_fake():
    realm = exp.load_realm()
    groups = {}
    for i, path in enumerate(["/kh-all", "/kh-all-approvers", "/kh-all/team"]):
        members = [
            {"id": x["username"], **{k: v for k, v in x.items() if k != "groups"}} for x in exp.users_of(path)
        ]
        groups[f"g{i}"] = {"path": path, "members": members}
    assert {g["path"] for g in realm["groups"]} == {"/kh-all", "/kh-all-approvers"}
    return Fake(groups)


async def test_realm_fixture_matches_live_expectations():
    p, _ = provider(realm_fake())
    fetched = await p.fetch(exp.REFS)
    assert fetched == exp.expected_fetch()
    result = effective(exp.config(), fetched, exp.DOMAINS)
    assert result.members["g"] == exp.EXPECTED_MEMBERS
    assert result.approvers["g"] == exp.EXPECTED_APPROVERS
    assert {(s.role, s.who, s.reason) for s in result.skips} == exp.EXPECTED_SKIPS
