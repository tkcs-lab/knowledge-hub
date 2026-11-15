from __future__ import annotations

import pytest

from knowledge_hub.visibility import Visibility, derive_visibility


def test_union_of_audiences_sorted_and_deduplicated():
    got = derive_visibility([Visibility(["mgmt", "all"]), Visibility(["hr", "all"])])
    assert got == Visibility(["all", "hr", "mgmt"], None)


def test_intersection_of_email_lists():
    got = derive_visibility([
        Visibility([], ["a@example.org", "b@example.org"]),
        Visibility([], ["b@example.org", "c@example.org"]),
    ])
    assert got == Visibility([], ["b@example.org"])


def test_null_email_lists_do_not_restrict_the_intersection():
    got = derive_visibility([Visibility(["mgmt"], None), Visibility([], ["a@example.org"])])
    assert got == Visibility(["mgmt"], ["a@example.org"])


def test_all_null_gives_null():
    assert derive_visibility([Visibility(["all"]), Visibility(["hr"])]).allowed_emails is None


def test_disjoint_lists_give_empty_not_null():
    got = derive_visibility([Visibility([], ["a@example.org"]), Visibility([], ["b@example.org"])])
    assert got.allowed_emails == []
    assert got.allowed_emails is not None


def test_empty_list_source_means_nobody():
    got = derive_visibility([Visibility(["all"], []), Visibility(["all"], None)])
    assert got == Visibility(["all"], [])


def test_three_sources():
    got = derive_visibility([
        Visibility(["all"], ["a@example.org", "b@example.org", "c@example.org"]),
        Visibility(["mgmt"], None),
        Visibility(["hr"], ["c@example.org", "a@example.org"]),
    ])
    assert got == Visibility(["all", "hr", "mgmt"], ["a@example.org", "c@example.org"])


def test_single_source_is_returned_normalised():
    assert derive_visibility([Visibility(["hr", "all", "hr"], ["b@x.org", "a@x.org"])]) == Visibility(
        ["all", "hr"], ["a@x.org", "b@x.org"])


def test_no_sources_is_an_error():
    with pytest.raises(ValueError):
        derive_visibility([])
