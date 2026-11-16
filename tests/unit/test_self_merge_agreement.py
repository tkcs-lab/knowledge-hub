"""The trust walk and ``kh domain-owners`` agree on the shared cases (plan 3.8)."""

import pytest

from knowledge_hub.forgejo import ForgejoClient
from knowledge_hub.governance.ci import CiError, run_domain_owners
from knowledge_hub.indexer.sync_plan import build_sync_plan
from tests.support.self_merge_cases import (
    REPO,
    build_domain_owners_world,
    build_trust_world,
    load_cases,
    make_fake,
)

pytestmark = pytest.mark.unit

CASES = {c["name"]: c for c in load_cases()}
NAMES = sorted(CASES)


async def ci_outcome(case) -> str:
    fake, config = build_domain_owners_world(case)
    read = ForgejoClient("http://f", "t", "kh", transport=fake.transport)
    try:
        result = await run_domain_owners(read, read, config, REPO, 7, post=False)
    except CiError:
        return "error"
    return "pass" if result.ok else "fail"


async def walk_outcome(case) -> str:
    repos, config = build_trust_world(case)
    fake = make_fake(case, repos)
    plan = await build_sync_plan(fake, config, REPO, None, {})
    stamps = {u.stamp or "disputed" for u in plan.upserts if u.commit_sha == "m1"}
    assert len(stamps) == 1, stamps
    return stamps.pop()


@pytest.mark.parametrize("name", NAMES)
async def test_domain_owners_matches_the_case(name):
    assert await ci_outcome(CASES[name]) == CASES[name]["domain_owners"]


@pytest.mark.parametrize("name", NAMES)
async def test_trust_walk_matches_the_case(name):
    assert await walk_outcome(CASES[name]) == CASES[name]["trust"]


@pytest.mark.parametrize("name", NAMES)
async def test_ci_is_never_looser_than_the_walk(name):
    case = CASES[name]
    if case["trust"] == "owner_merge" and not case.get("approvals"):
        assert case["domain_owners"] == "pass"
    if "ci_gap" not in case:
        assert not (case["domain_owners"] == "pass" and case["trust"] == "disputed")
