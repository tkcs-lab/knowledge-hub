from __future__ import annotations

import pytest

from knowledge_hub.approvals import BOT_LOGINS, current_approvals
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
