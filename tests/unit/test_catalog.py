"""build_groups and build_repos: pure answers of list_groups and list_repos."""

from __future__ import annotations

import pytest

from knowledge_hub.config import parse_config
from knowledge_hub.directory.model import Membership
from knowledge_hub.tools.catalog import build_groups, build_repos

pytestmark = pytest.mark.unit

CONFIG = parse_config(
    {
        "people": {"alice": "alice@example.com", "Bob": "bob@example.com", "carol": "carol@example.com"},
        "labels": {"ember": "Ember", "old": "Retired"},
        "archived_labels": ["old"],
        "tech_org": "kh-tech",
        "groups": {
            "zeta": {
                "repo": "kb-zeta",
                "members": ["alice"],
                "approvers": ["alice"],
                "slack_channel": "#zeta",
                "directory": {
                    "keycloak": {"approvers": ["z-leads"]},
                    "google": {"approvers": ["b@x.com", "a@x.com"]},
                },
            },
            "all": {"members": ["alice", "Bob"], "approvers": ["alice"]},
            "gone": {"members": ["carol"], "approvers": ["carol"], "archived": True},
        },
        "tech_repos": {
            "zzz": {"labels": ["ember", "old"], "dirs": ["docs", "adr"]},
            "aaa": {"audience": "zeta"},
        },
    }
)

MEMBERSHIP = Membership(
    people={"alice": "alice@example.com", "Bob": "bob@example.com"},
    members={"all": frozenset({"alice", "bob"}), "zeta": frozenset({"alice"})},
    approvers={
        "all": frozenset({"zed", "bob", "alice"}),  # zed is not in people; bob differs in case
        "zeta": frozenset({"alice"}),
        "gone": frozenset({"carol"}),
    },
)


def test_groups_sorted_without_archived():
    out = build_groups(CONFIG, MEMBERSHIP)
    assert [g["name"] for g in out["groups"]] == ["all", "zeta"]


def test_group_entry_shape_and_approvers():
    all_, zeta = build_groups(CONFIG, MEMBERSHIP)["groups"]
    assert all_ == {
        "name": "all",
        "repo": "kb-all",
        "slack_channel": None,
        "approvers": [
            {"login": "alice", "email": "alice@example.com"},
            {"login": "bob", "email": "bob@example.com"},
            {"login": "zed", "email": None},
        ],
        "approver_directory_groups": [],
    }
    assert zeta["slack_channel"] == "#zeta"
    assert zeta["approver_directory_groups"] == [
        {"provider": "google", "group": "b@x.com"},
        {"provider": "google", "group": "a@x.com"},
        {"provider": "keycloak", "group": "z-leads"},
    ]


def test_repos_with_index_data():
    state = {"kb-all": ("abc", "2026-01-01T00:00:00+00:00")}
    counts = {"kb-all": 3, "aaa": 2}
    out = build_repos(CONFIG, state, counts)
    assert out["tech_org"] == "kh-tech"
    assert [r["name"] for r in out["repos"]] == ["kb-all", "kb-zeta", "aaa", "zzz"]
    by = {r["name"]: r for r in out["repos"]}
    assert by["kb-all"] == {
        "name": "kb-all",
        "kind": "group",
        "audience": "all",
        "dirs": None,
        "labels": None,
        "documents": 3,
        "last_indexed_sha": "abc",
        "last_indexed_at": "2026-01-01T00:00:00+00:00",
    }
    assert by["kb-zeta"]["documents"] == 0
    assert by["kb-zeta"]["last_indexed_sha"] is None and by["kb-zeta"]["last_indexed_at"] is None
    assert (by["aaa"]["kind"], by["aaa"]["audience"], by["aaa"]["documents"]) == ("tech", "zeta", 2)
    assert by["zzz"]["audience"] == "all"
    assert by["zzz"]["dirs"] == ["docs", "adr"]
    assert by["zzz"]["labels"] == ["ember"]


def test_repos_without_index_data():
    out = build_repos(CONFIG, None, None)
    assert all(
        r["documents"] is None and r["last_indexed_sha"] is None and r["last_indexed_at"] is None
        for r in out["repos"]
    )
    assert [r["name"] for r in out["repos"]] == ["kb-all", "kb-zeta", "aaa", "zzz"]
