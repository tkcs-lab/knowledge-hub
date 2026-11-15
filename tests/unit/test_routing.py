"""route_target_repo (plan 3.8): where promoted content may go, never wider than its sources."""

from __future__ import annotations

import pytest

from knowledge_hub.config import Config
from knowledge_hub.mcp.errors import EMAIL_SOURCES, NO_REPO, SPAN, WIDER, KhError
from knowledge_hub.visibility import Visibility, route_target_repo


def v(*aud: str, emails: list[str] | None = None) -> Visibility:
    return Visibility(required_audiences=list(aud), allowed_emails=emails)


def cfg(**kw) -> Config:
    return Config(**kw)


def test_all_goes_to_canonical_all():
    assert route_target_repo([v("all")], cfg()) == "canonical-all"


def test_empty_union_goes_to_all_repo():
    assert route_target_repo([v()], cfg()) == "canonical-all"


def test_mgmt_goes_to_canonical_mgmt():
    assert route_target_repo([v("mgmt")], cfg()) == "canonical-mgmt"
    assert route_target_repo([v("mgmt"), v("mgmt")], cfg()) == "canonical-mgmt"


def test_all_and_mgmt_span_without_flag():
    with pytest.raises(KhError) as e:
        route_target_repo([v("all"), v("mgmt")], cfg())
    assert e.value.text == SPAN.format(a="all", b="mgmt")


def test_all_and_mgmt_routes_with_flag():
    c = cfg(restricted_audiences_imply_all=True)
    assert route_target_repo([v("all"), v("mgmt")], c) == "canonical-mgmt"


def test_mgmt_and_hr_refused_even_with_flag():
    for c in (cfg(), cfg(restricted_audiences_imply_all=True)):
        with pytest.raises(KhError) as e:
            route_target_repo([v("mgmt"), v("hr")], c)
        assert e.value.text == SPAN.format(a="hr", b="mgmt")


def test_email_restricted_refused():
    with pytest.raises(KhError) as e:
        route_target_repo([v("all"), v("all", emails=["a@x.y"])], cfg())
    assert e.value.text == EMAIL_SOURCES
    with pytest.raises(KhError):
        route_target_repo([v("all", emails=[])], cfg(), "canonical-all")


def test_audience_without_t0_repo():
    c = cfg(repos={"canonical-all": {"tier": "T0", "audience": "all"}})
    with pytest.raises(KhError) as e:
        route_target_repo([v("hr")], c)
    assert e.value.text == NO_REPO.format(a="hr")


def test_explicit_wider_repo_refused():
    with pytest.raises(KhError) as e:
        route_target_repo([v("mgmt")], cfg(), "canonical-all")
    assert e.value.text == WIDER.format(repo="canonical-all", a="all", u="mgmt")


def test_explicit_mgmt_with_all_sources_refused_without_flag():
    with pytest.raises(KhError) as e:
        route_target_repo([v("all")], cfg(), "canonical-mgmt")
    assert e.value.text == WIDER.format(repo="canonical-mgmt", a="mgmt", u="all")


def test_explicit_mgmt_with_empty_union_accepted():
    assert route_target_repo([v()], cfg(), "canonical-mgmt") == "canonical-mgmt"


def test_explicit_matching_repo_accepted():
    assert route_target_repo([v("mgmt")], cfg(), "canonical-mgmt") == "canonical-mgmt"
    flag = cfg(restricted_audiences_imply_all=True)
    assert route_target_repo([v("all"), v("mgmt")], flag, "canonical-mgmt") == "canonical-mgmt"


def test_flag_never_empties_a_lone_all():
    with pytest.raises(KhError) as e:
        route_target_repo([v("all")], cfg(restricted_audiences_imply_all=True), "canonical-mgmt")
    assert e.value.text == WIDER.format(repo="canonical-mgmt", a="mgmt", u="all")


def test_t1_repo_is_never_an_automatic_target():
    repos = {
        "notes-all": {"tier": "T1", "audience": "all"},
        "canonical-all": {"tier": "T0", "audience": "all"},
    }
    assert route_target_repo([v("all")], cfg(repos=repos)) == "canonical-all"
    with pytest.raises(KhError):
        route_target_repo([v("all")], cfg(repos={"notes-all": repos["notes-all"]}))


def test_explicit_unknown_repo_is_no_repo():
    with pytest.raises(KhError) as e:
        route_target_repo([v()], cfg(), "nope")
    assert e.value.text == NO_REPO.format(a="nope")
