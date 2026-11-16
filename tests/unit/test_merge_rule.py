import pytest

from knowledge_hub import merge_rule as mr
from knowledge_hub.governance.owners import build_owners

pytestmark = pytest.mark.unit

OWNERS = build_owners(
    "all",
    portfolio_team="owners-portfolio",
    product_team="owners-product",
    technical_team="owners-technical",
    owner_team="owners-all",
    admin_team="kh-admins",
)


def check(**kw):
    args = dict(
        author="ann",
        merger="ann",
        bots=("kh-hub-bot",),
        owners=OWNERS,
        paths=("portfolio/a.md",),
        self_merge_teams=["owners-portfolio"],
        author_teams=["owners-portfolio"],
    )
    args.update(kw)
    return mr.self_merge_check(**args)


def test_qualifies():
    r = check()
    assert r.ok and r.reason is None and r.uncovered == ()


def test_other_team_path_is_uncovered():
    r = check(paths=("portfolio/a.md", "product/b.md"))
    assert not r.ok and r.reason == mr.NOT_OWNER and r.uncovered == ("product/b.md",)


def test_team_not_in_self_merge_teams():
    r = check(self_merge_teams=["owners-product"])
    assert r.reason == mr.NOT_OWNER and r.uncovered == ("portfolio/a.md",)


def test_author_not_in_team():
    assert check(author_teams=[]).reason == mr.NOT_OWNER


def test_other_merger():
    assert check(merger="bob").reason == mr.OTHER_MERGER


def test_bot_author_fails_even_in_team():
    r = check(author="kh-hub-bot", merger="kh-hub-bot")
    assert r.reason == mr.BOT_AUTHOR
    r = check(author="ci-bot", merger="ci-bot", bots=("kh-hub-bot", "ci-bot"))
    assert r.reason == mr.BOT_AUTHOR


def test_bot_compared_case_folded():
    assert mr.is_bot("KH-Hub-Bot", ("kh-hub-bot",))
    assert not mr.is_bot(None, ("kh-hub-bot",))
    assert check(author="CI-Bot", merger="ci-bot", bots=("ci-bot",)).reason == mr.BOT_AUTHOR


def test_no_owners():
    assert check(owners=None).reason == mr.NO_OWNERS


def test_not_enabled():
    assert check(self_merge_teams=[]).reason == mr.NOT_ENABLED


def test_no_paths():
    assert check(paths=()).reason == mr.NO_PATHS


def test_missing_merger():
    assert check(merger=None).reason == mr.NO_MERGER
    assert check(author=None).reason == mr.NO_MERGER


@pytest.mark.parametrize("path", ["owners.yml", "schema/x.json", ".forgejo/workflows/kh.yml", "glossary.md"])
def test_governance_paths_need_admins(path):
    both = ["owners-portfolio", "kh-admins"]
    assert check(paths=(path,), self_merge_teams=both, author_teams=both).ok
    assert (
        check(paths=(path,), self_merge_teams=["owners-portfolio"], author_teams=both).reason == mr.NOT_OWNER
    )
    assert (
        check(paths=(path,), self_merge_teams=both, author_teams=["owners-portfolio"]).reason == mr.NOT_OWNER
    )


def test_rename_needs_old_path_owned():
    paths = mr.touched_paths([{"filename": "portfolio/new.md", "previous_filename": "product/old.md"}])
    assert paths == ("portfolio/new.md", "product/old.md")
    r = check(paths=paths)
    assert not r.ok and r.uncovered == ("product/old.md",)
    paths = mr.touched_paths([{"filename": "portfolio/new.md", "previous_filename": "portfolio/old.md"}])
    assert check(paths=paths).ok


def test_touched_paths_sorted_unique():
    got = mr.touched_paths(
        [{"filename": "b"}, {"filename": "a", "previous_filename": "b"}, {"filename": "a"}]
    )
    assert got == ("a", "b")


def test_equal_ids_pass_with_different_logins():
    assert check(author="ann", merger="ann2", author_id=7, merger_id=7).ok


def test_different_ids_fail_with_same_login():
    assert check(author_id=7, merger_id=8).reason == mr.OTHER_MERGER


def test_ghost_gives_no_merger():
    r = check(author="Ghost", merger="Ghost", author_id=-1, merger_id=-1)
    assert r.reason == mr.NO_MERGER
    assert not mr.same_person("Ghost", "ghost")


def test_zero_ids_fall_back_to_logins():
    assert check(author="ann", merger="bob", author_id=0, merger_id=0).reason == mr.OTHER_MERGER
    assert check(author="ann", merger="ann", author_id=0, merger_id=0).ok


def test_case_different_login_same_user():
    assert check(author="Ann", merger="ann").ok
    assert mr.same_person("Ann", "ann", 5, None)


def test_teams_to_check():
    t = ["owners-portfolio", "owners-product"]
    assert mr.teams_to_check(None, ["portfolio/a.md"], t) == frozenset()
    assert mr.teams_to_check(OWNERS, [], t) == frozenset()
    assert mr.teams_to_check(OWNERS, ["portfolio/a.md", "product/b.md"], t) == frozenset(t)
    assert mr.teams_to_check(OWNERS, ["portfolio/a.md", "technical/c.md"], t) is None
    assert mr.teams_to_check(OWNERS, ["portfolio/a.md"], []) is None
