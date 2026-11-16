from __future__ import annotations

import pytest

from knowledge_hub.approvals import BOT_LOGINS, current_approvals, is_proposal, requester_from_statuses
from knowledge_hub.proposals import NO_REQUESTER
from tests.support.forgejo_fake import approval

PR = {"head": {"sha": "h1"}, "user": {"login": "alice"}}


def test_counts_good_review():
    good = approval("h1", "bob")
    assert current_approvals(PR, [good], BOT_LOGINS) == [good]


@pytest.mark.parametrize(
    "change",
    [
        {"state": "COMMENT"},
        {"official": False},
        {"dismissed": True},
        {"commit_id": "old"},
        {"user": {"login": "alice"}},
        {"user": {"login": "kh-hub-bot"}},
    ],
    ids=["not-approved", "not-official", "dismissed", "other-commit", "author", "bot"],
)
def test_excluded(change):
    review = {**approval("h1", "bob"), **change}
    assert current_approvals(PR, [review], BOT_LOGINS) == []


def test_exclude_logins_casefolded():
    review = approval("h1", "Bob")
    assert current_approvals(PR, [review], BOT_LOGINS, exclude_logins=["bOb"]) == []
    assert current_approvals(PR, [review], BOT_LOGINS, exclude_logins=["carol"]) == [review]


def test_author_and_bot_casefolded():
    assert current_approvals(PR, [approval("h1", "ALICE")], BOT_LOGINS) == []
    assert current_approvals(PR, [approval("h1", "KH-Hub-Bot")], BOT_LOGINS) == []


def test_is_proposal():
    assert not is_proposal(PR)
    assert not is_proposal({"head": {"ref": "feature/x"}})
    assert is_proposal({"head": {"ref": "bot/propose/abc"}})


UUID = "11111111-1111-1111-1111-111111111111"
PROP = {"head": {"sha": "h1", "ref": f"bot/propose/{UUID}"}}


def st(login="bob", uuid=UUID, *, creator="kh-hub-bot", state="success", context="kh/requested-by"):
    return {"context": context, "state": state, "creator": creator, "description": f"{login} {uuid}"}


def test_requester_valid():
    assert requester_from_statuses(PROP, [st()]) == (("bob",), None)
    assert requester_from_statuses(PROP, [st(), st()]) == (("bob",), None)


def test_requester_missing():
    assert requester_from_statuses(PROP, []) == ((), NO_REQUESTER)
    assert requester_from_statuses(PROP, [st(state="failure"), st(context="other")]) == ((), NO_REQUESTER)


def test_requester_from_non_poster():
    assert requester_from_statuses(PROP, [st(creator="mallory")]) == ((), NO_REQUESTER)


def test_requester_uuid_of_other_proposal():
    other = "22222222-2222-2222-2222-222222222222"
    assert requester_from_statuses(PROP, [st(uuid=other)]) == ((), NO_REQUESTER)


def test_requester_two_logins():
    assert requester_from_statuses(PROP, [st("bob"), st("carol")]) == ((), NO_REQUESTER)


def test_requester_malformed_description():
    bad = {**st(), "description": "bob"}
    assert requester_from_statuses(PROP, [bad]) == ((), NO_REQUESTER)


def test_requester_other_pr_ignores_uuid_and_has_no_problem():
    assert requester_from_statuses(PR, []) == ((), None)
    assert requester_from_statuses(PR, [st("bob", "x"), st("carol", "y"), st("Bob", "z")]) == (
        ("bob", "carol"),
        None,
    )
    assert requester_from_statuses(PR, [st(creator="mallory")]) == ((), None)
