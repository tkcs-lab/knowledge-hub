from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_hub.admin import ops, yamledit
from knowledge_hub.admin.ops import OpError

TEXT = """\
# top comment
org: kh   # the org
people:
  alice: alice@example.com   # alice
  Bob: bob@example.com
  carol: carol@example.com
groups:
  all:
    repo: kb-all
    members: [alice, Bob]
    approvers: [alice]   # keep
  mgmt:
    members: [alice, Bob]
    approvers: [alice, Bob]
labels:
  a: Alpha   # first
  b: Beta
# archived_labels: [b]
"""

TECH = """\
org: kh
tech_org: kh-tech
tech_repos:
  core:
    labels: [a]
people:
  alice: alice@example.com
groups:
  all:
    members: [alice]
    approvers: [alice]
labels:
  a: Alpha
  b: Beta
"""


def load(tmp_path: Path, text: str = TEXT) -> yamledit.Doc:
    p = tmp_path / "kh.yaml"
    p.write_text(text)
    return yamledit.load(p)


def check(doc: yamledit.Doc) -> str:
    text = yamledit.render(doc)
    yamledit.validate(text)
    return text


def refuses(fn, *args, text: str) -> None:
    with pytest.raises(OpError) as exc:
        fn(*args)
    assert str(exc.value) == text


def test_label_add(tmp_path):
    doc = load(tmp_path)
    assert ops.label_add(doc, "c", "Gamma")
    assert "  c: Gamma\n" in check(doc)


def test_label_add_creates_missing_labels(tmp_path):
    doc = load(tmp_path, "org: kh\n")
    assert ops.label_add(doc, "c", "")
    assert yamledit.validate(yamledit.render(doc)).labels == {"c": ""}


def test_label_add_already_exists(tmp_path):
    refuses(ops.label_add, load(tmp_path), "a", "x", text="label a already exists")


def test_label_describe(tmp_path):
    doc = load(tmp_path)
    assert ops.label_describe(doc, "a", "New")
    assert "a: New" in check(doc)
    assert not ops.label_describe(doc, "a", "New")


def test_label_describe_unknown_label(tmp_path):
    refuses(ops.label_describe, load(tmp_path), "zz", "x", text="unknown label zz")


def test_label_remove(tmp_path):
    doc = load(tmp_path)
    assert ops.label_remove(doc, "b")
    assert "b: Beta" not in check(doc)


def test_label_remove_unknown_label(tmp_path):
    refuses(ops.label_remove, load(tmp_path), "zz", text="unknown label zz")


def test_label_remove_also_drops_archived_entry(tmp_path):
    doc = load(tmp_path)
    ops.label_archive(doc, "a")
    ops.label_archive(doc, "b")
    assert ops.label_remove(doc, "b")
    cfg = yamledit.validate(yamledit.render(doc))
    assert cfg.archived_labels == ["a"]


def test_label_remove_refuses_label_of_a_tech_repo(tmp_path):
    refuses(
        ops.label_remove,
        load(tmp_path, TECH),
        "a",
        text="label a is used by tech repo core; remove it from tech_repos first",
    )
    doc = load(tmp_path, TECH)
    assert ops.label_remove(doc, "b")
    check(doc)


def test_label_archive_and_unarchive(tmp_path):
    doc = load(tmp_path)
    assert ops.label_archive(doc, "a")
    assert "archived_labels: [a]" in check(doc)
    assert not ops.label_archive(doc, "a")
    assert ops.label_unarchive(doc, "a")
    check(doc)
    assert not ops.label_unarchive(doc, "a")


def test_label_archive_unknown_label(tmp_path):
    refuses(ops.label_archive, load(tmp_path), "zz", text="unknown label zz")


def test_label_unarchive_unknown_label(tmp_path):
    refuses(ops.label_unarchive, load(tmp_path), "zz", text="unknown label zz")


def test_group_add(tmp_path):
    doc = load(tmp_path)
    assert ops.group_add(doc, "new", ["alice", "BOB"], ["alice"], "kb-new", None)
    cfg = yamledit.validate(yamledit.render(doc))
    assert cfg.groups["new"].members == ["alice", "Bob"]


def test_group_add_already_exists(tmp_path):
    refuses(
        ops.group_add,
        load(tmp_path),
        "all",
        ["alice"],
        ["alice"],
        None,
        None,
        text="group all already exists",
    )


def test_group_add_creates_missing_groups(tmp_path):
    doc = load(tmp_path, "people:\n  alice: a@example.com\n")
    assert ops.group_add(doc, "g", ["alice"], ["alice"], None, None)
    check(doc)


def test_group_add_writes_flow_lists(tmp_path):
    doc = load(tmp_path)
    ops.group_add(doc, "new", ["alice", "Bob"], ["alice"], None, None)
    text = check(doc)
    assert "    members: [alice, Bob]\n" in text
    assert "    approvers: [alice]\n" in text


def test_group_add_writes_slack_channel_last(tmp_path):
    doc = load(tmp_path)
    ops.group_add(doc, "new", ["alice"], ["alice"], "kb-new", "#chan")
    text = check(doc)
    block = text[text.index("  new:") :].splitlines()
    assert [ln.split(":")[0].strip() for ln in block[:5]] == [
        "new",
        "repo",
        "members",
        "approvers",
        "slack_channel",
    ]


def test_group_remove(tmp_path):
    doc = load(tmp_path)
    assert ops.group_remove(doc, "mgmt")
    assert "mgmt" not in check(doc)


def test_group_remove_unknown_group(tmp_path):
    refuses(ops.group_remove, load(tmp_path), "zz", text="unknown group zz")


def test_group_set_archived(tmp_path):
    doc = load(tmp_path)
    assert ops.group_set_archived(doc, "mgmt", True)
    assert "archived: true" in check(doc)
    assert not ops.group_set_archived(doc, "mgmt", True)
    assert ops.group_set_archived(doc, "mgmt", False)
    assert "archived: true" not in check(doc)
    assert not ops.group_set_archived(doc, "mgmt", False)


def test_group_set_archived_unknown_group(tmp_path):
    refuses(ops.group_set_archived, load(tmp_path), "zz", True, text="unknown group zz")


def test_member_add(tmp_path):
    doc = load(tmp_path)
    assert ops.member_add(doc, "all", "CAROL")
    assert yamledit.validate(yamledit.render(doc)).groups["all"].members == [
        "alice",
        "Bob",
        "carol",
    ]
    assert not ops.member_add(doc, "all", "carol")


def test_member_add_unknown_group(tmp_path):
    refuses(ops.member_add, load(tmp_path), "zz", "alice", text="unknown group zz")


def test_member_add_login_not_in_people(tmp_path):
    refuses(
        ops.member_add,
        load(tmp_path),
        "all",
        "dave",
        text="login dave is not in people; run kh person add first",
    )


def test_member_remove(tmp_path):
    doc = load(tmp_path)
    assert ops.member_remove(doc, "all", "bob", False)
    check(doc)


def test_member_remove_not_a_member(tmp_path):
    refuses(
        ops.member_remove,
        load(tmp_path),
        "all",
        "carol",
        False,
        text="carol is not a member of all",
    )


def test_member_remove_is_an_approver(tmp_path):
    refuses(
        ops.member_remove,
        load(tmp_path),
        "all",
        "alice",
        False,
        text="alice is an approver of all; remove the approver first or pass --with-approver",
    )


def test_member_remove_with_approver_flag_removes_both(tmp_path):
    doc = load(tmp_path)
    assert ops.member_remove(doc, "mgmt", "bob", True)
    cfg = yamledit.validate(yamledit.render(doc))
    assert cfg.groups["mgmt"].members == ["alice"]
    assert cfg.groups["mgmt"].approvers == ["alice"]


def test_member_remove_with_approver_flag_last_approver(tmp_path):
    refuses(
        ops.member_remove,
        load(tmp_path),
        "all",
        "alice",
        True,
        text="group all needs at least one approver",
    )


def test_approver_add(tmp_path):
    doc = load(tmp_path)
    assert ops.approver_add(doc, "all", "BOB")
    check(doc)
    assert not ops.approver_add(doc, "all", "bob")


def test_approver_add_not_a_member(tmp_path):
    refuses(
        ops.approver_add,
        load(tmp_path),
        "all",
        "carol",
        text="carol is not a member of all",
    )


def test_approver_remove(tmp_path):
    doc = load(tmp_path)
    assert ops.approver_remove(doc, "mgmt", "bob")
    check(doc)


def test_approver_remove_not_an_approver(tmp_path):
    refuses(
        ops.approver_remove,
        load(tmp_path),
        "all",
        "bob",
        text="bob is not an approver of all",
    )


def test_approver_remove_needs_at_least_one_approver(tmp_path):
    refuses(
        ops.approver_remove,
        load(tmp_path),
        "all",
        "alice",
        text="group all needs at least one approver",
    )


def test_person_add(tmp_path):
    doc = load(tmp_path)
    assert ops.person_add(doc, "dave", "Dave@Example.COM")
    assert "  dave: dave@example.com\n" in check(doc)


def test_person_add_already_exists(tmp_path):
    refuses(ops.person_add, load(tmp_path), "BOB", "x@example.com", text="person BOB already exists")


def test_person_remove(tmp_path):
    doc = load(tmp_path)
    assert ops.person_remove(doc, "carol", False)
    assert "carol" not in check(doc)


def test_person_remove_unknown_person(tmp_path):
    refuses(ops.person_remove, load(tmp_path), "zz", False, text="unknown person zz")


def test_person_remove_last_approver(tmp_path):
    doc = load(tmp_path)
    before = yamledit.render(doc)
    refuses(
        ops.person_remove,
        doc,
        "alice",
        True,
        text="person alice is the last approver of all; add another approver first",
    )
    assert yamledit.render(doc) == before


def test_person_remove_member_of_groups(tmp_path):
    refuses(
        ops.person_remove,
        load(tmp_path),
        "bob",
        False,
        text="person Bob is a member of all, mgmt; pass --from-groups to remove them from every group",
    )


def test_person_remove_from_groups_cleans_every_group(tmp_path):
    doc = load(tmp_path)
    assert ops.person_remove(doc, "bob", True)
    cfg = yamledit.validate(yamledit.render(doc))
    assert "Bob" not in cfg.people
    for g in cfg.groups.values():
        assert "Bob" not in g.members
        assert "Bob" not in g.approvers


def test_ops_keep_untouched_comments(tmp_path):
    doc = load(tmp_path)
    ops.label_add(doc, "c", "Gamma")
    ops.member_add(doc, "mgmt", "carol")
    ops.person_add(doc, "dave", "d@example.com")
    text = check(doc)
    for kept in ("# top comment", "# the org", "# alice", "# keep", "# first"):
        assert kept in text


# tech repos

NO_TECH = """\
# keep me
org: kh
tech_org: kh-tech
people:
  alice: alice@example.com
groups:
  all:
    members: [alice]
    approvers: [alice]
labels:
  a: Alpha
"""


def test_repo_add_with_and_without_keys(tmp_path):
    doc = load(tmp_path, NO_TECH)
    assert ops.repo_add(doc, "plain", None, None)
    assert ops.repo_add(doc, "full", ["docs", "adr"], ["a"])
    text = check(doc)
    assert "  plain: {}\n" in text
    assert "  full:\n    labels: [a]\n    dirs: [docs, adr]\n" in text
    assert "# keep me" in text


def test_repo_add_with_audience_writes_the_key(tmp_path):
    doc = load(tmp_path, NO_TECH)
    assert ops.repo_add(doc, "aud", ["docs"], None, "all")
    assert "  aud:\n    dirs: [docs]\n    audience: all\n" in check(doc)


def test_repo_add_with_unknown_audience_raises(tmp_path):
    doc = load(tmp_path, NO_TECH)
    refuses(ops.repo_add, doc, "x", None, None, "nope", text="unknown group nope")


def test_repo_add_refusals(tmp_path):
    doc = load(tmp_path)
    refuses(ops.repo_add, doc, "x", None, None, text="tech_org is not set; add it to kh.yaml first")
    doc = load(tmp_path, TECH)
    refuses(ops.repo_add, doc, "core", None, None, text="tech repo core already exists")
    refuses(ops.repo_add, doc, "n", None, ["zz"], text="unknown label zz")


def test_repo_set_changes_and_noops(tmp_path):
    doc = load(tmp_path, TECH)
    assert not ops.repo_set(doc, "core", None, None)
    assert not ops.repo_set(doc, "core", ["docs"], ["a"])
    assert ops.repo_set(doc, "core", ["docs", "adr"], ["b"])
    text = check(doc)
    assert "    labels: [b]\n    dirs: [docs, adr]\n" in text
    assert ops.repo_set(doc, "core", None, [])
    assert "labels: []" in check(doc)
    assert not ops.repo_set(doc, "core", None, [])


def test_repo_set_audience(tmp_path):
    mgmt = "groups:\n  mgmt:\n    members: [alice]\n    approvers: [alice]\n"
    doc = load(tmp_path, TECH.replace("groups:\n", mgmt))
    assert not ops.repo_set(doc, "core", None, None, "all")
    assert ops.repo_set(doc, "core", None, None, "mgmt")
    assert "audience: mgmt" in check(doc)
    assert not ops.repo_set(doc, "core", None, None, "mgmt")
    refuses(ops.repo_set, doc, "core", None, None, "nope", text="unknown group nope")


def test_repo_set_on_a_flow_entry_writes_block_style(tmp_path):
    doc = load(tmp_path, TECH.replace("  core:\n    labels: [a]\n", "  core: {}\n"))
    assert ops.repo_set(doc, "core", ["adr"], None)
    assert "  core:\n    dirs: [adr]\n" in check(doc)


def test_repo_set_refusals(tmp_path):
    doc = load(tmp_path, TECH)
    refuses(ops.repo_set, doc, "nope", None, None, text="unknown tech repo nope")
    refuses(ops.repo_set, doc, "core", None, ["zz"], text="unknown label zz")


def test_repo_remove(tmp_path):
    doc = load(tmp_path, TECH)
    refuses(ops.repo_remove, doc, "nope", text="unknown tech repo nope")
    assert ops.repo_remove(doc, "core")
    assert "core" not in check(doc).split("people:")[0]


def test_repo_ops_keep_comments(tmp_path):
    doc = load(tmp_path, NO_TECH)
    ops.repo_add(doc, "r", None, None)
    assert "# keep me" in check(doc)
