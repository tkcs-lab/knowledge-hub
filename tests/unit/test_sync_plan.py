from __future__ import annotations

from knowledge_hub.config import parse_config
from knowledge_hub.indexer.sync_plan import build_sync_plan
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake, approval, pr

CFG = parse_config(
    {
        "repos": {
            "canonical-all": {"tier": "T0", "audience": "all", "trust_anchor": "a0"},
            "canonical-mgmt": {"tier": "T0", "audience": "mgmt", "trust_anchor": "a0"},
            "notes-all": {"tier": "T1", "audience": "all"},
        }
    }
)


def doc(id_: str, extra: str = "", title: str = "T") -> str:
    head = f"---\nid: {id_}\ntitle: {title}\ndomain: product\nreview_by: 2027-01-01\n"
    return f"{head}{extra}---\nBody of {id_}.\n"


def merged(
    sha: str, parent: str, files: dict, number: int, approve: bool = True, date="2026-11-02T10:00:00Z"
):
    """A merge commit plus its PR and reviews."""
    head = f"h{number}"
    commit = FakeCommit(sha, [parent, head], files, date)
    return commit, pr(number, sha, head), ([approval(head, "bob", date)] if approve else [])


def repo_of(commits_prs: list, root_files: dict | None = None) -> FakeRepo:
    commits = [FakeCommit("a0", [], root_files or {})]
    prs, reviews = [], {}
    for c, p, r in commits_prs:
        commits.append(c)
        prs.append(p)
        reviews[p["number"]] = r
    return FakeRepo(commits, prs, reviews)


async def plan(repos, repo="canonical-all", state=None, existing=None):
    return await build_sync_plan(ForgejoFake(repos), CFG, repo, state, existing or {})


def up(p, key):
    return next(u for u in p.upserts if u.doc_key == key)


async def test_approval_fields_and_visibility():
    r = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1)])
    p = await plan({"canonical-all": r})
    u = up(p, "a")
    assert (u.status, u.approved_by, u.approved_at) == ("current", ["bob"], "2026-11-02T10:00:00Z")
    assert (u.merge_pr, u.pr_url, u.commit_sha) == (1, "https://forgejo.test/kh/repo/pulls/1", "m1")
    assert (u.tier, u.required_audiences, u.allowed_emails) == ("T0", ["all"], None)
    assert [v.commit_sha for v in p.versions] == ["m1"]
    assert p.repo_state == {
        "repo": "canonical-all",
        "trusted": True,
        "untrusted_reason": None,
        "last_indexed_sha": "m1",
        "trust_anchor_sha": "a0",
    }


async def test_file_without_approved_pr_is_disputed_with_alert():
    r = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1, approve=False)])
    p = await plan({"canonical-all": r})
    u = up(p, "a")
    assert u.status == "disputed" and u.approved_by == [] and u.merge_pr == 1
    assert any(
        a.path == "a.md" and a.message == "file a.md on canonical-all/main has no approved merge PR"
        for a in p.alerts
    )


async def test_frontmatter_tier_is_removed_with_alert():
    r = repo_of([merged("m1", "a0", {"a.md": doc("a", "tier: T0\n")}, 1)])
    existing = {"a": {"path": "a.md", "commit_sha": "a0"}}
    p = await plan({"canonical-all": r}, existing=existing)
    assert p.upserts == [] and p.removals == ["a"] and p.deletions == []
    assert [a.message for a in p.alerts] == [
        "frontmatter must not set tier; the server decides tier and audience"
    ]


async def test_rename_keeps_doc_key():
    r = repo_of(
        [
            merged("m1", "a0", {"old.md": doc("a")}, 1),
            merged("m2", "m1", {"old.md": None, "new.md": doc("a")}, 2),
        ]
    )
    existing = {"a": {"path": "old.md", "commit_sha": "m1"}}
    p = await plan({"canonical-all": r}, state={"last_indexed_sha": "m1"}, existing=existing)
    assert [(x.doc_key, x.old_path, x.new_path) for x in p.renames] == [("a", "old.md", "new.md")]
    assert up(p, "a").path == "new.md" and p.deletions == []


async def test_missing_file_is_deleted():
    r = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1)])
    existing = {"a": {"path": "a.md"}, "gone": {"path": "gone.md"}}
    p = await plan({"canonical-all": r}, existing=existing)
    assert p.deletions == ["gone"] and p.removals == []


async def test_untrusted_repo_plans_everything_disputed():
    squash = FakeCommit("s1", ["a0"], {"a.md": doc("a")})
    r = FakeRepo([FakeCommit("a0", []), squash])
    p = await plan({"canonical-all": r})
    assert up(p, "a").status == "disputed" and up(p, "a").approved_by == []
    assert p.repo_state["trusted"] is False
    assert p.repo_state["untrusted_reason"] == "non-merge commit s1 on main of canonical-all"
    assert "non-merge commit s1 on main of canonical-all" in [a.message for a in p.alerts]


async def test_cross_repo_supersedes_is_disputed_and_touches_nothing_else():
    mgmt = repo_of([merged("m1", "a0", {"x.md": doc("x", "supersedes: old-all\n")}, 1)])
    allr = repo_of([merged("m1", "a0", {"old.md": doc("old-all")}, 1)])
    repos = {"canonical-mgmt": mgmt, "canonical-all": allr}
    p = await plan(repos, repo="canonical-mgmt")
    assert up(p, "x").status == "disputed"
    assert "supersedes/superseded_by must name a document in the same repo" in [a.message for a in p.alerts]
    assert {u.doc_key for u in p.upserts} == {"x"} and p.repo == "canonical-mgmt"
    assert (await plan(repos, repo="canonical-all")).upserts[0].status == "current"


async def test_same_repo_supersedes_marks_old_superseded():
    r = repo_of([merged("m1", "a0", {"o.md": doc("old"), "n.md": doc("new", "supersedes: old\n")}, 1)])
    p = await plan({"canonical-all": r})
    assert (up(p, "old").status, up(p, "old").superseded_by) == ("superseded", "new")
    assert up(p, "new").status == "current"


async def test_disputed_doc_cannot_supersede():
    r = repo_of(
        [merged("m1", "a0", {"o.md": doc("old"), "n.md": doc("new", "supersedes: old\n")}, 1, approve=False)]
    )
    assert up(await plan({"canonical-all": r}), "old").status == "disputed"
    r = repo_of(
        [
            merged("m1", "a0", {"o.md": doc("old")}, 1),
            merged("m2", "m1", {"n.md": doc("new", "supersedes: old\n")}, 2, approve=False),
        ]
    )
    p = await plan({"canonical-all": r})
    assert up(p, "old").status == "current" and up(p, "new").status == "disputed"


async def test_duplicate_id_all_rejected_when_none_or_many_approved():
    r = repo_of([merged("m1", "a0", {"a.md": doc("d"), "b.md": doc("d")}, 1)])
    p = await plan({"canonical-all": r}, existing={"d": {"path": "a.md"}})
    assert p.upserts == [] and p.removals == ["d"]
    msgs = {a.message for a in p.alerts}
    assert msgs == {"duplicate id d in canonical-all: a.md, b.md"}


async def test_duplicate_id_keeps_only_the_approved_one():
    r = repo_of(
        [merged("m1", "a0", {"a.md": doc("d")}, 1), merged("m2", "m1", {"b.md": doc("d")}, 2, approve=False)]
    )
    p = await plan({"canonical-all": r})
    assert [(u.path, u.status) for u in p.upserts] == [("a.md", "current")]
    assert any(a.path == "b.md" and "duplicate id d" in a.message for a in p.alerts)


async def test_rewritten_main_gives_empty_plan_and_alert():
    r = repo_of([merged("m1", "a0", {"a.md": doc("a")}, 1)])
    p = await plan({"canonical-all": r}, state={"last_indexed_sha": "zzz"}, existing={"a": {"path": "a.md"}})
    assert p.refused and p.upserts == p.deletions == p.removals == p.versions == []
    assert p.repo_state is None
    assert [a.message for a in p.alerts] == [
        "main of canonical-all was rewritten; run kh rebuild after review"
    ]


async def test_notes_repo_needs_no_approval():
    r = FakeRepo([FakeCommit("n1", [], {"a.md": doc("a")})])
    p = await plan({"notes-all": r}, repo="notes-all")
    u = up(p, "a")
    assert (u.tier, u.status, u.approved_by, u.commit_sha) == ("T1", "current", [], "n1")
    assert p.repo_state["trusted"] is True
