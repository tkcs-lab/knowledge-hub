import pytest

from knowledge_hub.config import (
    AUDIENCE_RE,
    GROUP_RE,
    LABEL_RE,
    LOGIN_RE,
    ConfigError,
    KhConfig,
    load_config,
    parse_config,
)

pytestmark = pytest.mark.unit

PEOPLE = {"alice": "alice@example.com", "bob": "Bob@Example.com"}
GROUPS = {
    "all": {"members": ["alice", "bob"], "approvers": ["alice"]},
    "mgmt": {"repo": "mgmt-docs", "members": ["alice"], "approvers": ["alice"]},
}


def cfg(**over):
    data = {"people": PEOPLE, "groups": GROUPS}
    data.update(over)
    return parse_config(data)


def refused(data, text):
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert str(exc.value).startswith("config:")
    assert text in str(exc.value)


def test_default_config_is_valid():
    c = KhConfig()
    assert (c.org, c.bot_login, c.indexer_login) == ("kh", "kh-bot", "kh-indexer")
    assert c.forgejo_web_url is None and c.hub_url is None
    assert c.people == {} and c.groups == {} and c.labels == {}
    assert load_config() == c and load_config(None) == c
    assert parse_config(None) == c
    assert c.bots == frozenset({"kh-bot", "kh-indexer"})
    assert GROUP_RE.pattern == AUDIENCE_RE.pattern


def test_regexes():
    assert LABEL_RE.fullmatch("product:ember") and not LABEL_RE.fullmatch("Product")
    assert LOGIN_RE.fullmatch("a.b-c_d") and not LOGIN_RE.fullmatch("-a") and not LOGIN_RE.fullmatch("a" * 40)


def test_valid_config_and_repo_default():
    c = cfg(labels={"product:ember": "Ember"})
    assert c.repo_for_group("all") == "kb-all" and c.repo_for_group("mgmt") == "mgmt-docs"
    assert c.group_for_repo("kb-all") == "all" and c.group_for_repo("mgmt-docs") == "mgmt"
    assert c.group_for_repo("nope") is None
    assert c.people["bob"] == "bob@example.com"


def test_unknown_key_refused():
    refused({"nope": 1}, "nope")
    refused({"groups": {"all": {"approvers": [], "members": [], "x": 1}}}, "x")


@pytest.mark.parametrize("name", ["Bad", "-a", "a b", "x" * 64, "Owners", "owners"])
def test_group_name_rules(name):
    refused({"groups": {name: {"members": [], "approvers": ["a"]}}}, "group")


def test_approve_collision():
    groups = {
        "all": {"members": ["alice"], "approvers": ["alice"]},
        "all-approve": {"members": ["alice"], "approvers": ["alice"]},
    }
    refused({"people": PEOPLE, "groups": groups}, "all-approve")


def test_logins_valid_and_unique_casefolded():
    refused({"people": {"-bad": "a@b.c"}}, "login")
    refused({"people": {"Alice": "a@x.io", "alice": "b@x.io"}}, "twice")
    refused(
        {"people": PEOPLE, "groups": {"g": {"members": ["alice", "ALICE"], "approvers": ["alice"]}}},
        "twice",
    )
    group = {"members": ["alice"], "approvers": ["ALICE"]}
    c = parse_config({"people": {"Alice": "a@x.io"}, "groups": {"g": group}})
    assert c.audiences_for_email("a@x.io") == {"g"}


def test_approvers_non_empty_and_subset():
    refused({"people": PEOPLE, "groups": {"g": {"members": ["alice"], "approvers": []}}}, "approver")
    refused(
        {"people": PEOPLE, "groups": {"g": {"members": ["alice"], "approvers": ["bob"]}}},
        "config: group g approver bob is not a member",
    )


def test_repo_names():
    group = {"repo": "Bad Repo", "members": [], "approvers": ["alice"]}
    refused({"people": PEOPLE, "groups": {"g": group}}, "repo")
    groups = {
        "a": {"repo": "same", "members": ["alice"], "approvers": ["alice"]},
        "b": {"repo": "same", "members": ["alice"], "approvers": ["alice"]},
    }
    refused({"people": PEOPLE, "groups": groups}, "same")
    groups = {
        "a": {"members": ["alice"], "approvers": ["alice"]},
        "b": {"repo": "kb-a", "members": ["alice"], "approvers": ["alice"]},
    }
    refused({"people": PEOPLE, "groups": groups}, "kb-a")


def test_bots():
    refused({"bot_login": "same", "indexer_login": "same"}, "differ")
    refused({"bot_login": "-x"}, "login")
    refused({"people": {"kh-bot": "b@x.io"}}, "bot")
    refused({"people": {"KH-Indexer": "b@x.io"}}, "bot")
    group = {"members": ["bot"], "approvers": ["bot"]}
    refused({"people": PEOPLE, "bot_login": "bot", "groups": {"g": group}}, "bot")
    assert parse_config({"bot_login": "b1", "indexer_login": "i1"}).bots == frozenset({"b1", "i1"})


def test_labels():
    assert cfg(labels={"a": "x" * 200}).labels["a"] == "x" * 200
    refused({"labels": {"Bad": "x"}}, "label")
    refused({"labels": {"a": "x" * 201}}, "description")
    refused({"labels": {f"l{i}": "" for i in range(501)}}, "500")
    assert len(parse_config({"labels": {f"l{i}": "" for i in range(500)}}).labels) == 500


def test_forgejo_web_url():
    assert parse_config({"forgejo_web_url": "https://f.example.com"}).forgejo_web_url
    refused({"forgejo_web_url": "http://f.example.com"}, "forgejo_web_url")


def test_hub_url():
    assert parse_config({"hub_url": "http://knowledge-hub:8080"}).hub_url
    assert parse_config({"hub_url": "https://hub.example.com/"}).hub_url
    refused({"hub_url": "ftp://hub"}, "hub_url")
    refused({"hub_url": "http://hub/path"}, "hub_url")
    refused({"hub_url": "hub:8080"}, "hub_url")


def test_people_emails():
    refused({"people": {"a": "no-at"}}, "email")
    refused({"people": {"a": "x@y@z"}}, "email")
    refused({"people": {"a": "x" * 250 + "@y.io"}}, "email")
    refused({"people": {"a": "X@Y.io", "b": "x@y.IO"}}, "config: email x@y.io belongs to two people")
    assert len(parse_config({"people": {"a": "x" * 249 + "@y.io"}}).people["a"]) == 254


def test_group_member_must_be_in_people():
    refused(
        {"people": PEOPLE, "groups": {"g": {"members": ["zed"], "approvers": ["zed"]}}},
        "config: group g member zed is not in people",
    )


def test_audiences_for_email():
    c = cfg()
    assert c.audiences_for_email("alice@example.com") == {"all", "mgmt"}
    assert c.audiences_for_email("bob@example.com") == {"all"}
    assert c.audiences_for_email("BOB@EXAMPLE.COM") == {"all"}
    assert c.audiences_for_email("stranger@example.com") == frozenset()
    assert c.audiences_for_email(None) == frozenset()
    assert c.audiences_for_email("") == frozenset()
    solo = parse_config({"people": {"dave": "dave@example.com"}})
    assert solo.audiences_for_email("dave@example.com") == frozenset()


def test_load_config_errors(tmp_path):
    p = tmp_path / "kh.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ConfigError, match="^config: top level"):
        load_config(p)
    p.write_text("a: [\n")
    with pytest.raises(ConfigError, match="^config: invalid YAML"):
        load_config(p)
    with pytest.raises(ConfigError, match="^config: cannot read"):
        load_config(tmp_path / "missing.yaml")
    p.write_text("")
    assert load_config(p) == KhConfig()
    p.write_text("org: x\npeople: {a: a@x.io}\n")
    assert load_config(p).org == "x"


def test_archived_group_flag_defaults_false_and_is_read():
    c = cfg(groups={**GROUPS, "old": {"members": ["bob"], "approvers": ["bob"], "archived": True}})
    assert c.groups["all"].archived is False
    assert c.groups["old"].archived is True
    refused({"groups": {"g": {"approvers": [], "archived": "maybe"}}}, "archived")


def test_archived_labels_must_be_defined_and_unique():
    assert cfg().archived_labels == []
    assert cfg(labels={"a": "A"}, archived_labels=["a"]).archived_labels == ["a"]
    refused(
        {"labels": {"a": "A"}, "archived_labels": ["b"]},
        "config: archived_labels entry 'b' is not in labels",
    )
    refused(
        {"labels": {"a": "A"}, "archived_labels": ["a", "a"]},
        "config: archived_labels lists 'a' twice",
    )


def test_active_groups_and_visible_labels():
    c = cfg(
        groups={**GROUPS, "old": {"members": ["bob"], "approvers": ["bob"], "archived": True}},
        labels={"a": "A", "b": "B", "c": "C"},
        archived_labels=["b"],
    )
    assert c.active_groups == ["all", "mgmt"]
    assert c.is_active("all") is True
    assert c.is_active("old") is False
    assert c.is_active("missing") is False
    assert c.archived_label_set == frozenset({"b"})
    assert c.visible_labels(["c", "b", "a", "zzz"]) == ["c", "a", "zzz"]
    assert cfg().visible_labels(["x", "y"]) == ["x", "y"]


def test_audiences_for_email_skips_archived_groups():
    c = cfg(groups={**GROUPS, "old": {"members": ["alice", "bob"], "approvers": ["alice"], "archived": True}})
    assert c.audiences_for_email("alice@example.com") == {"all", "mgmt"}
    assert c.audiences_for_email("bob@example.com") == {"all"}


TECH = {"tech_org": "kh-tech", "tech_repos": {"ember-core": {"labels": ["ember"]}}}


def tech_cfg(**over):
    data = {"labels": {"ember": "Ember"}, **TECH}
    data.update(over)
    return cfg(**data)


def test_tech_repos_need_tech_org():
    refused(
        {"people": PEOPLE, "groups": GROUPS, "tech_repos": {"ember-core": {}}},
        "config: tech_repos needs tech_org",
    )
    refused({"tech_org": "-bad"}, "tech_org '-bad' is not a valid login")


def test_tech_org_must_differ_from_org():
    refused({"org": "KH", "tech_org": "kh"}, "config: tech_org must differ from org")


def test_tech_repo_cannot_be_a_group_repo():
    refused(
        {"people": PEOPLE, "groups": GROUPS, "tech_org": "t", "tech_repos": {"mgmt-docs": {}}},
        "config: tech repo mgmt-docs is also the repo of group mgmt",
    )
    old = {**GROUPS, "old": {"members": ["bob"], "approvers": ["bob"], "archived": True}}
    refused(
        {"people": PEOPLE, "groups": old, "tech_org": "t", "tech_repos": {"kb-old": {}}},
        "config: tech repo kb-old is also the repo of group old",
    )
    refused(
        {"people": PEOPLE, "groups": GROUPS, "tech_org": "t", "tech_repos": {"Bad": {}}},
        "config: tech repo 'Bad' is not a valid repository name",
    )


def test_tech_repos_need_group_all():
    groups = {"mgmt": GROUPS["mgmt"]}
    refused(
        {"people": PEOPLE, "groups": groups, "tech_org": "t", "tech_repos": {"r": {}}},
        "config: tech_repos needs the group all",
    )


def test_tech_repos_need_active_group_all():
    groups = {**GROUPS, "all": {**GROUPS["all"], "archived": True}}
    refused(
        {"people": PEOPLE, "groups": groups, "tech_org": "t", "tech_repos": {"r": {}}},
        "config: tech_repos needs the group all to be active",
    )


def test_tech_repo_label_must_exist():
    refused(
        {"people": PEOPLE, "groups": GROUPS, "tech_org": "t", "tech_repos": {"r": {"labels": ["x"]}}},
        "config: tech repo r label 'x' is not in labels",
    )


def test_tech_repo_archived_label_is_accepted():
    c = tech_cfg(archived_labels=["ember"])
    assert c.tech_labels("ember-core") == ["ember"]


def test_tech_repo_label_listed_twice():
    refused(
        {
            "people": PEOPLE,
            "groups": GROUPS,
            "labels": {"a": "A"},
            "tech_org": "t",
            "tech_repos": {"r": {"labels": ["a", "a"]}},
        },
        "config: tech repo r lists label 'a' twice",
    )


def test_tech_helpers():
    c = tech_cfg(tech_repos={"zeta": {}, "ember-core": {"labels": ["ember"]}})
    assert c.is_tech_repo("ember-core") and not c.is_tech_repo("kb-all")
    assert c.tech_labels("ember-core") == ["ember"] and c.tech_labels("zeta") == []
    assert c.tech_repo_names == ["ember-core", "zeta"]


def test_config_without_tech_keys_is_unchanged():
    c = cfg()
    assert c.tech_org is None and c.tech_repos == {}
    assert c.tech_repo_names == [] and not c.is_tech_repo("kb-all")
