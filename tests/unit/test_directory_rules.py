import pytest

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.directory.model import DirectoryUser, GroupFetch, Membership, Skip
from knowledge_hub.directory.rules import classify, effective, login_from_email
from tests.support.principals import KH_CONFIG

pytestmark = pytest.mark.unit

DOMAINS = ["x.org"]
RESERVED = ("kh-bot", "kh-indexer", "owners")


def user(email, *, enabled=True, verified=True, username=""):
    return DirectoryUser(email=email, enabled=enabled, email_verified=verified, username=username)


def cfg(people=None, members=None, approvers=None, archived=False):
    people = people if people is not None else {"sam": "sam@x.org"}
    return KhConfig(
        people=people,
        groups={
            "g": GroupConfig(
                members=members if members is not None else ["sam"],
                approvers=approvers if approvers is not None else ["sam"],
                archived=archived,
            )
        },
    )


def fetch(members=(), approvers=()):
    return {"g": GroupFetch(members=tuple(members), approvers=tuple(approvers))}


@pytest.mark.parametrize(
    ("u", "reason"),
    [
        (user("a@x.org", enabled=False), "disabled"),
        (user("a@x.org", verified=False), "email not verified"),
        (user(""), "no valid email"),
        (user("a@b@x.org"), "no valid email"),
        (user("a@evil.org"), "domain not allowed"),
        (user("a+b@x.org"), "not a valid login"),
        (user(".a@x.org"), "not a valid login"),
        (user("owners@x.org"), "bot or reserved login"),
        (user("KH-Bot@x.org"), "bot or reserved login"),
        (user("ok@X.org"), ""),
    ],
)
def test_classify_reasons(u, reason):
    assert classify(u, DOMAINS, RESERVED) == reason


def test_classify_reason_order():
    assert classify(user("a@evil.org", enabled=False, verified=False), DOMAINS, RESERVED) == "disabled"
    assert classify(user("a@evil.org", verified=False), DOMAINS, RESERVED) == "email not verified"


@pytest.mark.parametrize(
    ("email", "login"),
    [
        ("Anna.Kiss@Example.com", "anna.kiss"),
        ("a" * 39 + "@x.org", "a" * 39),
        ("a" * 40 + "@x.org", None),
        (".a@x.org", None),
        ("a+b@x.org", None),
        ("", None),
        ("a@b@x.org", None),
        ("Owners@dom", "owners"),
    ],
)
def test_login_from_email(email, login):
    assert login_from_email(email) == login


def test_reserved_directory_login_is_skipped():
    m = effective(cfg(), fetch([user("Owners@dom.org")]), ["dom.org"])
    assert Skip("g", "member", "owners@dom.org", "bot or reserved login") in m.skips
    assert "owners" not in m.people


def test_each_skip_reason_is_recorded():
    users = [
        user("a@x.org", enabled=False),
        user("b@x.org", verified=False),
        user(
            "",
        ),
        user("c@evil.org"),
        user("d+e@x.org"),
        user("kh-bot@x.org"),
    ]
    m = effective(cfg(), fetch(users), DOMAINS)
    assert {s.reason for s in m.skips} == {
        "disabled",
        "email not verified",
        "no valid email",
        "domain not allowed",
        "not a valid login",
        "bot or reserved login",
    }
    assert set(m.members["g"]) == {"sam"}


def test_passing_directory_member_joins():
    m = effective(cfg(), fetch([user("Anna.Kiss@X.org")]), DOMAINS)
    assert m.people["anna.kiss"] == "anna.kiss@x.org"
    assert m.members["g"] == frozenset({"sam", "anna.kiss"})


def test_collision_between_directory_emails_skips_both():
    m = effective(cfg(), fetch([user("a@x.org"), user("a@y.org")]), ["x.org", "y.org"])
    assert "a" not in m.people
    reasons = {(s.who, s.reason) for s in m.skips}
    assert reasons == {
        ("a@x.org", "login shared by several emails"),
        ("a@y.org", "login shared by several emails"),
    }


def test_static_login_wins_over_directory_email():
    m = effective(cfg(), fetch([user("sam@y.org")]), ["x.org", "y.org"])
    assert m.people == {"sam": "sam@x.org"}
    assert Skip("g", "member", "sam@y.org", "login shared by several emails") in m.skips


def test_same_email_different_case_is_one_person():
    c = cfg(people={"Anna": "Anna@X.org"}, members=["Anna"], approvers=["Anna"])
    m = effective(c, fetch([user("anna@x.org")]), DOMAINS)
    assert m.skips == ()
    assert m.members["g"] == frozenset({"Anna"})
    assert len(m.people) == 1


def test_directory_email_held_by_static_entry_takes_its_login():
    c = cfg(people={"sam": "sam@x.org", "boss": "boss@x.org"}, members=["sam"], approvers=["sam"])
    m = effective(c, fetch([user("boss@x.org")]), DOMAINS)
    assert m.members["g"] == frozenset({"sam", "boss"})
    assert m.people["boss"] == "boss@x.org"
    assert "boss" in m.people and len(m.people) == 2


def test_duplicate_user_gives_one_member_and_one_skip():
    m = effective(cfg(), fetch([user("a@x.org"), user("a@x.org")]), DOMAINS)
    assert m.members["g"] == frozenset({"sam", "a"})
    m2 = effective(cfg(), fetch([user("z@evil.org"), user("z@evil.org")]), DOMAINS)
    assert len([s for s in m2.skips if s.who == "z@evil.org"]) == 1


def test_approver_who_is_not_a_member_is_skipped():
    m = effective(cfg(), fetch(approvers=[user("boss@x.org")]), DOMAINS)
    assert m.approvers["g"] == frozenset({"sam"})
    assert Skip("g", "approver", "boss@x.org", "approver is not a member") in m.skips


def test_directory_approver_who_is_a_member_is_kept():
    m = effective(cfg(), fetch([user("boss@x.org")], [user("boss@x.org")]), DOMAINS)
    assert m.approvers["g"] == frozenset({"sam", "boss"})


def test_static_approver_not_member_is_dropped():
    c = KhConfig(
        people={"sam": "sam@x.org", "ann": "ann@x.org"},
        groups={
            "g": GroupConfig(
                members=["sam"], approvers=["sam", "ann"], directory={"google": {"members": ["g@x.org"]}}
            )
        },
    )
    m = Membership.from_config(c)
    assert m.approvers["g"] == frozenset({"sam"})
    assert Skip("g", "approver", "ann@x.org", "approver is not a member") in m.skips


def test_static_entries_survive_an_empty_fetch():
    m = effective(cfg(), fetch(), DOMAINS)
    assert m.members["g"] == frozenset({"sam"})
    assert m.approvers["g"] == frozenset({"sam"})
    assert m.skips == ()


def test_archived_group_gets_no_directory_data():
    m = effective(cfg(archived=True), fetch([user("a@x.org")]), DOMAINS)
    assert m.members["g"] == frozenset({"sam"})
    assert "a" not in m.people


def test_group_absent_from_fetched_keeps_static_only():
    c = KhConfig(
        people={"sam": "sam@x.org"},
        groups={
            "g": GroupConfig(members=["sam"], approvers=["sam"]),
            "h": GroupConfig(members=["sam"], approvers=["sam"]),
        },
    )
    m = effective(c, {"g": GroupFetch((user("a@x.org"),), ())}, DOMAINS)
    assert m.members["g"] == frozenset({"sam", "a"})
    assert m.members["h"] == frozenset({"sam"})


def test_changed_domains_remove_a_user_on_the_same_fetch():
    f = fetch([user("a@x.org")])
    assert "a" in effective(cfg(), f, ["x.org"]).members["g"]
    assert "a" not in effective(cfg(), f, ["other.org"]).members["g"]


def test_none_fetched_equals_from_config():
    assert effective(KH_CONFIG, None, DOMAINS) == Membership.from_config(KH_CONFIG)


def test_skips_are_sorted():
    m = effective(cfg(), fetch([user("z@evil.org"), user("b@evil.org")]), DOMAINS)
    assert list(m.skips) == sorted(m.skips, key=lambda s: (s.group, s.role, s.who, s.reason))


def test_audiences_match_config_for_every_person():
    m = Membership.from_config(KH_CONFIG)
    for email in [*KH_CONFIG.people.values(), None, "", "nobody@example.test", "ALICE@example.test"]:
        assert m.audiences_for_email(email, KH_CONFIG.is_active) == KH_CONFIG.audiences_for_email(email)


def test_audiences_exclude_inactive_groups():
    assert (
        Membership.from_config(KH_CONFIG).audiences_for_email("alice@example.test", lambda g: False)
        == frozenset()
    )
