import pytest

from knowledge_hub import cli
from knowledge_hub.config import Config
from knowledge_hub.forgejo import ForgejoClient
from knowledge_hub.governance.ci import CiError, run_domain_owners
from knowledge_hub.governance.owners import build_owners, dump_owners, load_owners
from knowledge_hub.governance.owners_check import check_owners
from tests.support.forgejo_fake import approval
from tests.support.forgejo_http import FakeForgejoHttp

pytestmark = pytest.mark.unit

CONFIG = Config()
OWNERS = build_owners(
    "all",
    portfolio_team="owners-portfolio",
    product_team="owners-product",
    technical_team="owners-technical",
    owner_team=None,
    admin_team="kh-admins",
)
OWNERS_YML = dump_owners(OWNERS)
TEAMS = {
    "owners-portfolio": [],
    "owners-product": ["bob"],
    "owners-technical": ["carol"],
    "kh-admins": ["dave"],
    "owners-hr": ["erin"],
}
REASON = "Reason for widening: HR needs this"


def make(
    paths, reviews=(), *, labels=(), body="", state="open", teams=None, previous=None, ref=None, statuses=()
):
    pr = {
        "number": 7,
        "state": state,
        "body": body,
        "user": {"login": "alice"},
        "head": {"sha": "h1", "ref": ref},
        "base": {"sha": "b1"},
        "merge_base": "m1",
        "labels": [{"name": n} for n in labels],
    }
    compare = [{"filename": p, "status": "modified"} for p in paths]
    for old, new in (previous or {}).items():
        compare.append({"filename": new, "status": "renamed", "previous_filename": old})
    world = {
        "canonical-all": {
            "sha": "s",
            "tree": {"owners.yml": OWNERS_YML},
            "compare": compare,
            "pulls": {7: pr},
            "reviews": {7: list(reviews)},
            "statuses": {"h1": list(statuses)},
        }
    }
    return FakeForgejoHttp(world, teams=TEAMS if teams is None else teams)


HR_CONFIG = Config(governance={"owner_teams": {"hr": ["owners-hr"]}})


async def run(fake, *, post=True, config=CONFIG):
    read = ForgejoClient("http://f", "t", "kh", transport=fake.transport)
    return await run_domain_owners(read, read, config, "canonical-all", 7, post=post)


def ok(user, sha="h1"):
    return approval(sha, user)


async def test_product_owner_does_not_unlock_technical_but_technical_does():
    path = "technical/x/design/a.md"
    result = await run(make([path], [ok("bob")]))
    assert not result.ok and result.lines == ["technical/**: approval from owners-technical required"]
    assert (await run(make([path], [ok("carol")]))).ok


async def test_author_and_bot_do_not_count():
    teams = {**TEAMS, "owners-product": ["bob", "alice", "kh-hub-bot"]}
    paths = ["product/a.md"]
    assert not (await run(make(paths, [ok("alice")], teams=teams))).ok
    assert not (await run(make(paths, [ok("kh-hub-bot")], teams=teams))).ok


async def test_stale_dismissed_and_unofficial_do_not_count():
    paths = ["product/a.md"]
    for review in (
        ok("bob", "old"),
        {**ok("bob"), "dismissed": True},
        {**ok("bob"), "official": False},
    ):
        assert not (await run(make(paths, [review]))).ok
    assert (await run(make(paths, [ok("bob")]))).ok


async def test_schema_needs_admins():
    paths = ["schema/frontmatter.schema.json"]
    result = await run(make(paths, [ok("bob")]))
    assert result.lines == ["schema/**: approval from kh-admins required"]
    assert (await run(make(paths, [ok("dave")]))).ok


async def test_both_teams_needed():
    paths = ["product/a.md", "technical/b.md"]
    assert (await run(make(paths, [ok("bob")]))).lines == [
        "technical/**: approval from owners-technical required"
    ]
    assert (await run(make(paths, [ok("bob"), ok("carol")]))).ok


async def test_widen_hr_team_required():
    paths = ["product/a.md"]
    labels = ["widen-audience/hr"]
    result = await run(make(paths, [ok("bob")], labels=labels, body=REASON), config=HR_CONFIG)
    assert result.lines == ["widen-audience needs approval from one of: owners-hr"]
    assert (await run(make(paths, [ok("bob"), ok("erin")], labels=labels, body=REASON), config=HR_CONFIG)).ok
    missing = await run(
        make(paths, [ok("bob"), ok("erin")], labels=labels, body="Reason for widening: <why>"),
        config=HR_CONFIG,
    )
    assert missing.lines == ["widen-audience needs a Reason for widening line"]
    bare = await run(make(paths, [ok("bob")], labels=["widen-audience"], body=REASON), config=HR_CONFIG)
    assert bare.lines == ["widen-audience needs a destination label widen-audience/<audience>"]
    unknown = await run(
        make(paths, [ok("bob")], labels=["widen-audience/zzz"], body=REASON), config=HR_CONFIG
    )
    assert unknown.lines == bare.lines


async def test_closed_pr_passes_without_status():
    fake = make(["product/a.md"], state="closed")
    result = await run(fake)
    assert result.ok and result.lines == ["pull request is not open"]
    assert fake.statuses == []


async def test_status_posted_to_head():
    fake = make(["product/a.md"], [ok("bob")])
    await run(fake)
    assert fake.statuses == [
        {"context": "kh/domain-owners", "state": "success", "description": "all checks passed"}
    ]
    assert fake.status_targets == [("canonical-all", "h1")]
    fail = make(["product/a.md"])
    await run(fail)
    assert fail.statuses == [{"context": "kh/domain-owners", "state": "failure", "description": "1 problems"}]
    quiet = make(["product/a.md"])
    await run(quiet, post=False)
    assert quiet.statuses == []


async def test_missing_team_is_error():
    teams = {k: v for k, v in TEAMS.items() if k != "owners-product"}
    fake = make(["product/a.md"], [ok("bob")], teams=teams)
    with pytest.raises(CiError):
        await run(fake)
    assert fake.statuses[-1]["state"] == "error"


def test_missing_team_exits_two(monkeypatch, tmp_path):
    teams = {k: v for k, v in TEAMS.items() if k != "owners-product"}
    fake = make(["product/a.md"], [ok("bob")], teams=teams)
    monkeypatch.setattr(
        cli, "_make_forgejo", lambda u, t, o: ForgejoClient(u, t, o, transport=fake.transport)
    )
    token = tmp_path / "t"
    token.write_text("tok")
    argv = [
        "domain-owners",
        "--repo",
        "canonical-all",
        "--pr",
        "7",
        "--forgejo-url",
        "http://f",
        "--token-file",
        str(token),
        "--no-status",
    ]
    assert cli.main(argv) == 2


def test_cli_exit_one(monkeypatch, tmp_path, capsys):
    fake = make(["product/a.md"])
    monkeypatch.setattr(
        cli, "_make_forgejo", lambda u, t, o: ForgejoClient(u, t, o, transport=fake.transport)
    )
    token = tmp_path / "t"
    token.write_text("tok")
    argv = [
        "domain-owners",
        "--repo",
        "canonical-all",
        "--pr",
        "7",
        "--forgejo-url",
        "http://f",
        "--token-file",
        str(token),
        "--no-status",
    ]
    assert cli.main(argv) == 1
    assert "product/**: approval from owners-product required" in capsys.readouterr().out


async def test_rename_counts_both_paths():
    fake = make([], [ok("bob")], previous={"technical/old.md": "product/new.md"})
    result = await run(fake)
    assert result.lines == ["technical/**: approval from owners-technical required"]


def test_check_owners_text():
    owners = load_owners(OWNERS_YML)
    lines = check_owners(
        owners=owners,
        paths=["product/a.md", "product/b.md", "technical/c.md"],
        counted=[],
        members={},
        widen=("hr", ["owners-hr", "kh-admins"]),
        widen_reason_ok=False,
        widen_label_problem=None,
    )
    assert lines == [
        "product/**: approval from owners-product required",
        "technical/**: approval from owners-technical required",
        "widen-audience needs a Reason for widening line",
        "widen-audience needs approval from one of: kh-admins, owners-hr",
    ]
    bare = OWNERS.model_copy(update={"rules": OWNERS.rules[:1]})
    assert check_owners(
        owners=bare,
        paths=["x/y.md"],
        counted=[],
        members={},
        widen=None,
        widen_reason_ok=True,
        widen_label_problem="widen-audience needs a destination label widen-audience/<audience>",
    ) == [
        "widen-audience needs a destination label widen-audience/<audience>",
        "x/y.md: no owner in owners.yml",
    ]


UUID = "11111111-1111-1111-1111-111111111111"
PROP_REF = f"bot/propose/{UUID}"
PRODUCT_PATH = "product/x/design/a.md"


def record(login, uuid=UUID):
    return {
        "context": "kh/requested-by",
        "state": "success",
        "creator": "kh-hub-bot",
        "description": f"{login} {uuid}",
    }


async def test_requester_approval_does_not_count():
    fake = make([PRODUCT_PATH], [ok("bob")], ref=PROP_REF, statuses=[record("bob")])
    result = await run(fake)
    assert not result.ok and result.lines == ["product/**: approval from owners-product required"]


async def test_requester_exclusion_is_casefolded():
    fake = make([PRODUCT_PATH], [ok("bob")], ref=PROP_REF, statuses=[record("BOB")])
    assert not (await run(fake)).ok


async def test_other_owner_approval_counts_for_proposal():
    fake = make([PRODUCT_PATH], [ok("bob")], ref=PROP_REF, statuses=[record("alice2")], teams=TEAMS)
    assert (await run(fake)).ok


async def test_proposal_without_record_fails():
    from knowledge_hub.proposals import NO_REQUESTER

    result = await run(make([PRODUCT_PATH], [ok("bob")], ref=PROP_REF))
    assert not result.ok and NO_REQUESTER in result.lines


async def test_human_commit_on_top_fails():
    # the record belongs to an earlier head: statuses are read for h1 only, so none is found
    fake = make([PRODUCT_PATH], [ok("bob")], ref=PROP_REF)
    fake.repos["canonical-all"]["statuses"] = {"old-head": [record("bob")]}
    result = await run(fake)
    assert not result.ok and result.lines[-1].startswith("agent proposal has no valid requester")


async def test_other_branch_unaffected_without_record():
    assert (await run(make([PRODUCT_PATH], [ok("bob")], ref="feature/x"))).ok


async def test_other_branch_excludes_named_approver():
    fake = make([PRODUCT_PATH], [ok("bob")], ref="feature/x", statuses=[record("bob", "whatever")])
    result = await run(fake)
    assert not result.ok and result.lines == ["product/**: approval from owners-product required"]
