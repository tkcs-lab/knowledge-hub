from __future__ import annotations

import copy

import pytest

from knowledge_hub.indexer.sync_plan import build_sync_plan
from tests.support.self_merge_cases import (
    REPO,
    build_trust_world,
    load_cases,
    make_fake,
)

CASES = load_cases()
BY_NAME = {c["name"]: c for c in CASES}
IDS = list(BY_NAME)
STATUS = {"reviewed": "current", "owner_merge": "current", "disputed": "disputed"}


async def run(case, *, stored=None, rejudge=False, teams=None, strip=False):
    repos, config = build_trust_world(case)
    if strip:
        config = config.model_copy(update={"self_merge_teams": []})
    fake = make_fake(case, repos, teams)
    kwargs = {} if stored is None and not rejudge else {"stored_verdicts": stored, "rejudge": rejudge}
    plan = await build_sync_plan(fake, config, REPO, None, {}, **kwargs)
    return plan, fake


def touched(plan):
    return [u for u in plan.upserts if u.commit_sha == "m1"]


@pytest.mark.parametrize("name", IDS)
async def test_case_stamp_and_approvers(name):
    case = BY_NAME[name]
    plan, fake = await run(case)
    expected = case["trust"]
    docs = touched(plan)
    assert docs, "the case must change a document"
    for u in docs:
        assert u.status == STATUS[expected]
        assert u.stamp == (None if expected == "disputed" else expected)
        if expected == "owner_merge":
            assert u.approved_by == [case["merger"]]
        if expected == "reviewed":
            assert u.approved_by == sorted(case["approvals"])
        if expected == "disputed":
            assert u.approved_by == []
    if case.get("judged", True):
        v = plan.verdicts["m1"]
        assert v.stamp == (None if expected == "disputed" else expected)
        assert v.status == ("disputed" if expected == "disputed" else "trusted")
    else:
        assert "m1" not in plan.verdicts
    author = case["author"].casefold()
    if author in {"kh-hub-bot", *(b.casefold() for b in case.get("bots", []))}:
        assert "is_team_member" not in fake.calls


async def test_stored_verdicts_are_reused():
    case = BY_NAME["owner-self-merge-in-own-domain"]
    first, _ = await run(case)
    left = {t: m - {"alice"} for t, m in ((t, set(v)) for t, v in case["teams"].items())}
    plan, fake = await run(case, stored=first.verdicts, teams=left)
    assert [u.stamp for u in touched(plan)] == ["owner_merge"]
    assert [u.approved_by for u in touched(plan)] == [["alice"]]
    for call in ("pull_for_commit", "team_id", "is_team_member"):
        assert call not in fake.calls
    assert plan.verdicts == {}


async def test_rejudge_ignores_stored_verdicts():
    case = BY_NAME["owner-self-merge-in-own-domain"]
    first, _ = await run(case)
    left = {t: set(v) - {"alice"} for t, v in case["teams"].items()}
    plan, _ = await run(case, stored=first.verdicts, rejudge=True, teams=left)
    assert plan.rejudge is True
    assert [u.stamp for u in touched(plan)] == [None]
    assert [u.status for u in touched(plan)] == ["disputed"]
    assert plan.verdicts["m1"].status == "disputed"


async def test_empty_self_merge_teams_changes_nothing():
    case = copy.deepcopy(BY_NAME["owner-self-merge-in-own-domain"])
    case["self_merge_teams"] = []
    plain, fake = await run(case)
    with_args, _ = await run(case, stored={}, rejudge=False)
    assert plain.upserts == with_args.upserts
    assert plain.repo_state == with_args.repo_state
    assert "team_id" not in fake.calls and "is_team_member" not in fake.calls
    assert [u.status for u in touched(plain)] == ["disputed"]


def test_case_data_is_consistent():
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names))
    for c in CASES:
        assert c["trust"] in {"reviewed", "owner_merge", "disputed"}
        assert c["domain_owners"] in {"pass", "fail", "error"}
        if c["trust"] == "owner_merge" and not c.get("approvals"):
            assert c["domain_owners"] == "pass", c["name"]
        if c["trust"] == "disputed" and c["domain_owners"] == "pass":
            assert c.get("ci_gap"), c["name"]
