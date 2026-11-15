from __future__ import annotations

import json
from pathlib import Path

from knowledge_hub.indexer.trust import (
    DISPUTED,
    TRUSTED,
    TrustResult,
    evaluate_chain,
    files_from_compare,
)

DATA = Path(__file__).parent.parent / "data" / "forgejo"
ROOT, M1, M2 = "a" * 40, "b" * 40, "c" * 40


def run(name: str, **override) -> TrustResult:
    d = json.loads((DATA / f"{name}.json").read_text()) | override
    changed = {sha: files_from_compare(c) for sha, c in d["compare"].items()}
    reviews = {int(k): v for k, v in d["reviews"].items()}
    return evaluate_chain(d["repo"], d["commits"], d["pulls"], reviews, changed, d["bot_logins"], d["anchor"])


def test_all_approved_chain_is_trusted():
    r = run("all_approved")
    assert r.trusted and r.alerts == []
    assert r.file_verdicts["a.md"].status == TRUSTED
    assert r.file_verdicts["a.md"].approved_by == ("bob",)
    assert r.file_verdicts["a.md"].approved_at == "2026-11-02T10:00:00Z"
    assert r.file_verdicts["a.md"].merge_pr == 1
    assert r.file_verdicts["b.md"].approved_at == "2026-11-03T10:00:00Z"
    assert r.commit_verdicts[M2].status == TRUSTED


def test_self_approval_does_not_count():
    r = run("self_approval")
    assert r.file_verdicts["b.md"].status == DISPUTED
    assert r.file_verdicts["a.md"].status == TRUSTED
    assert "file b.md on kh/canonical-all/main has no approved merge PR" in r.alerts
    assert r.trusted


def test_dismissed_review_does_not_count():
    assert run("dismissed_review").file_verdicts["b.md"].status == DISPUTED


def test_bot_approval_does_not_count():
    assert run("bot_approval").file_verdicts["b.md"].status == DISPUTED


def test_approval_on_other_head_is_disputed():
    r = run("stale_head")
    assert r.file_verdicts["b.md"].status == DISPUTED
    assert r.file_verdicts["b.md"].reason == "no approval of the final head"


def test_non_merge_commit_makes_repo_untrusted_and_names_sha():
    r = run("squash_merge")
    assert not r.trusted
    assert f"non-merge commit {M2} on main of kh/canonical-all" in r.alerts
    assert r.file_verdicts["b.md"].status == DISPUTED
    assert r.file_verdicts["a.md"].status == DISPUTED  # untrusted repo: nothing is trusted


def test_new_root_with_null_anchor_is_untrusted():
    r = run("new_root_null_anchor")
    assert not r.trusted
    assert r.alerts == [f"non-merge commit {'d' * 40} on main of kh/canonical-all"]


def test_anchor_missing_from_chain_is_untrusted():
    r = run("anchor_missing")
    assert not r.trusted
    assert r.alerts == [f"trust anchor {'f' * 40} is not on main of kh/canonical-all"]
    assert r.verdict_for("a.md").status == DISPUTED


def test_file_before_anchor_is_trusted_as_trust_anchor():
    r = run("anchor_semantics")
    assert r.trusted
    v = r.file_verdicts["a.md"]  # changed by M1 == anchor
    assert (v.status, v.approved_by, v.approved_at) == (TRUSTED, ("trust-anchor",), "2026-11-02T11:00:00Z")
    assert r.verdict_for("never-touched.md").approved_by == ("trust-anchor",)
    assert r.file_verdicts["b.md"].approved_by == ("bob",)
    assert M1 in r.commit_verdicts and r.commit_verdicts[M1].approved_by == ("trust-anchor",)


def test_null_anchor_checks_every_commit_including_root():
    # all_approved has a merge-less root; with a null anchor the root commit fails the rule
    r = run("all_approved", anchor=None)
    assert not r.trusted
    assert f"non-merge commit {ROOT} on main of kh/canonical-all" in r.alerts
