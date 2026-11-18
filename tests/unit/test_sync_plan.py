"""Unit tests of the pure sync plan (plan section 3.6)."""

from __future__ import annotations

import pytest

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.indexer.sync_plan import MAX_FILE_BYTES, build_sync_plan, changed_paths
from tests.support.forgejo_fake import blob_sha, md

CFG = KhConfig(
    people={"alice": "alice@example.test"},
    groups={"all": GroupConfig(members=["alice"], approvers=["alice"])},
)
REPO = "kb-all"
HEAD = "h1"


def entry(path: str, text: str = "x", size: int | None = None, type_: str = "blob") -> dict:
    return {"path": path, "sha": blob_sha(text), "type": type_, "size": len(text) if size is None else size}


def plan(files: dict[str, str], stored=(), labels=("ember",), sizes=None):
    tree = [entry(p, t, (sizes or {}).get(p)) for p, t in files.items()]
    texts = {p: files[p] for p in changed_paths(tree, list(stored))}
    return build_sync_plan(CFG, REPO, HEAD, tree, list(stored), texts, labels)


def test_accepted_file_becomes_upsert():
    p = plan({"a.md": md("a", "Title", labels=["ember"])})
    [u] = p.upserts
    assert (u.repo, u.path, u.doc_key, u.audience, u.title, u.labels) == (
        REPO, "a.md", "a", "all", "Title", ["ember"],
    )  # fmt: skip
    assert u.commit_sha == HEAD and u.blob_sha == blob_sha(md("a", "Title", labels=["ember"]))
    assert p.deletes == [] and p.alerts == []


@pytest.mark.parametrize(
    "path",
    ["a.txt", "README.md", ".hidden.md", ".git/x.md", "docs/.draft/a.md", "docs/.a.md", "a.md.bak"],
)
def test_excluded_paths_are_ignored_without_alert(path):
    p = plan({path: md("a")})
    assert p.upserts == [] and p.alerts == []


def test_nested_readme_is_included():
    assert [u.path for u in plan({"docs/README.md": md("r")}).upserts] == ["docs/README.md"]


def test_trees_are_ignored():
    tree = [entry("docs", type_="tree"), entry("docs/a.md", md("a"))]
    p = build_sync_plan(CFG, REPO, HEAD, tree, [], {"docs/a.md": md("a")}, [])
    assert [u.path for u in p.upserts] == ["docs/a.md"]


def test_size_limit_uses_the_tree_size():
    p = plan(
        {"big.md": md("big"), "ok.md": md("ok")},
        sizes={"big.md": MAX_FILE_BYTES + 1, "ok.md": MAX_FILE_BYTES},
    )
    assert [u.path for u in p.upserts] == ["ok.md"]
    assert p.alerts == []


def test_oversize_stored_file_is_deleted():
    text = md("big")
    p = plan(
        {"big.md": text}, stored=[("big.md", blob_sha(text), "big")], sizes={"big.md": MAX_FILE_BYTES + 1}
    )
    assert p.deletes == ["big.md"] and p.upserts == []


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("no frontmatter", "missing frontmatter"),
        ("---\nid: a\ntitle: T\n---\n", "missing review_by"),
        (md("Bad Id"), "invalid id"),
        (md("a", review_by="2027-13-40"), "invalid review_by"),
    ],
)
def test_parse_errors_are_rejected_with_prefix(text, reason):
    p = plan({"a.md": text})
    assert p.upserts == []
    assert p.alerts == [("a.md", f"file rejected: {reason}")]


def test_unknown_label_rejects_the_file():
    p = plan({"a.md": md("a", labels=["ember", "nope"])})
    assert p.upserts == []
    assert p.alerts == [("a.md", "file rejected: unknown label nope")]


def test_duplicate_id_rejects_every_holder():
    p = plan({"a.md": md("same", "A"), "b.md": md("same", "B"), "c.md": md("other")})
    assert [u.path for u in p.upserts] == ["c.md"]
    assert sorted(p.alerts) == [
        ("a.md", "file rejected: duplicate id same"),
        ("b.md", "file rejected: duplicate id same"),
    ]


def test_duplicate_with_a_stored_unchanged_file_deletes_it():
    old = md("same", "Old")
    stored = [("a.md", blob_sha(old), "same")]
    p = plan({"a.md": old, "b.md": md("same", "New")}, stored=stored)
    assert p.upserts == []
    assert p.deletes == ["a.md"]
    assert sorted(p.alerts) == [
        ("a.md", "file rejected: duplicate id same"),
        ("b.md", "file rejected: duplicate id same"),
    ]


def test_unchanged_stored_file_is_not_upserted_nor_read():
    text = md("a")
    stored = [("a.md", blob_sha(text), "a")]
    tree = [entry("a.md", text)]
    assert changed_paths(tree, stored) == []
    p = build_sync_plan(CFG, REPO, HEAD, tree, stored, {}, [])
    assert p.upserts == [] and p.deletes == [] and p.alerts == []


def test_edited_file_is_upserted_and_stored_path_absent_is_deleted():
    stored = [("a.md", "old-blob", "a"), ("gone.md", "g", "g")]
    p = plan({"a.md": md("a", "New")}, stored=stored)
    assert [u.title for u in p.upserts] == ["New"]
    assert p.deletes == ["gone.md"]


def test_rename_deletes_the_old_path_and_upserts_the_new():
    text = md("a")
    p = plan({"b.md": text}, stored=[("a.md", blob_sha(text), "a")])
    assert [(u.path, u.doc_key) for u in p.upserts] == [("b.md", "a")]
    assert p.deletes == ["a.md"]


def test_changed_id_at_the_same_path_deletes_the_old_row():
    p = plan({"a.md": md("new")}, stored=[("a.md", "old-blob", "old")])
    assert [u.doc_key for u in p.upserts] == ["new"]
    assert p.deletes == ["a.md"]


def test_now_rejected_stored_file_is_deleted():
    p = plan({"a.md": md("a", labels=["nope"])}, stored=[("a.md", "old-blob", "a")])
    assert p.upserts == [] and p.deletes == ["a.md"]
    assert p.alerts == [("a.md", "file rejected: unknown label nope")]


def test_rejected_file_not_stored_is_retried_every_plan():
    files = {"a.md": md("a", labels=["later"])}
    assert plan(files).alerts == [("a.md", "file rejected: unknown label later")]
    again = plan(files, labels=("ember", "later"))
    assert [u.doc_key for u in again.upserts] == ["a"] and again.alerts == []


def test_repo_in_no_group_is_a_value_error():
    with pytest.raises(ValueError, match="no group"):
        build_sync_plan(CFG, "other", HEAD, [], [], {}, [])


def test_supersedes_reaches_the_upsert():
    p = plan({"a.md": md("a", supersedes=["x", "y"])})
    assert p.upserts[0].supersedes == ["x", "y"]
