"""Unit tests of the pure T0a document rules (plan section 3.4)."""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from knowledge_hub.config import GroupConfig, KhConfig, TechRepoConfig
from knowledge_hub.indexer.sync_plan import MAX_FILE_BYTES
from knowledge_hub.indexer.tech_docs import (
    MAX_TITLE,
    TECH_TIER,
    build_tech_plan,
    changed_tech_paths,
    is_tech_included,
    tech_doc_key,
    tech_review_by,
    tech_title,
)
from tests.support.forgejo_fake import blob_sha

CFG = KhConfig(
    org="kh",
    tech_org="kh-tech",
    people={"alice": "alice@example.test"},
    groups={"all": GroupConfig(members=["alice"], approvers=["alice"])},
    labels={"ember": "Ember", "other": "Other"},
    tech_repos={"ember-core": TechRepoConfig(labels=["ember"])},
)
REPO = "ember-core"
TODAY = dt.date(2026, 11, 18)


def entry(path: str, text: str = "x", size: int | None = None) -> dict:
    return {"path": path, "sha": blob_sha(text), "type": "blob", "size": len(text) if size is None else size}


def plan(files: dict, stored=(), labels=("ember",), texts=None, cfg=CFG):
    tree = [entry(p, t or "") for p, t in files.items()]
    if texts is None:
        texts = {p: files[p] for p in changed_tech_paths(tree, list(stored))}
    return build_tech_plan(cfg, REPO, "h1", tree, list(stored), texts, labels, TODAY)


def test_included_paths():
    for path in ("docs/a.md", "docs/x/y.md", "CLAUDE.md"):
        assert is_tech_included(entry(path)), path
    for path in ("README.md", "docs/.hidden/a.md", "src/a.md", "claude.md", "docs/a.txt"):
        assert not is_tech_included(entry(path)), path
    assert not is_tech_included(entry("docs/big.md", size=2 * 1024 * 1024))
    assert is_tech_included(entry("docs/ok.md", size=MAX_FILE_BYTES))


def test_key_is_the_path():
    assert tech_doc_key("docs/a.md") == "docs/a.md"


def test_long_key_is_exactly_100_chars_and_ends_with_a_hash():
    path = "docs/" + "a" * 150 + ".md"
    key = tech_doc_key(path)
    assert len(key) == 100
    assert key == path[:91] + "~" + hashlib.sha256(path.encode()).hexdigest()[:8]


def test_two_long_paths_with_one_prefix_get_different_keys():
    p1, p2 = "docs/" + "a" * 150 + "1.md", "docs/" + "a" * 150 + "2.md"
    assert tech_doc_key(p1) != tech_doc_key(p2)


def test_title_is_first_heading():
    assert tech_title(REPO, "docs/a.md", "intro\n## no\n#  Real title ##\n# later\n") == "Real title"


def test_title_skips_headings_inside_a_fence():
    assert tech_title(REPO, "docs/a.md", "```\n# not\n```\n~~~\n# nor\n~~~\n# yes\n") == "yes"


def test_title_falls_back_to_the_file_name():
    assert tech_title(REPO, "docs/x/setup.md", "no heading\n#nospace\n") == "setup"


def test_root_claude_md_title_has_the_repo():
    assert tech_title(REPO, "CLAUDE.md", "# Other\n") == "ember-core: CLAUDE.md"


def test_title_is_cut_at_200():
    assert len(tech_title(REPO, "docs/a.md", "# " + "x" * 300)) == MAX_TITLE


def test_review_by_is_100_years_ahead_and_survives_feb_29():
    assert tech_review_by(dt.date(2026, 11, 18)) == dt.date(2126, 11, 18)
    assert tech_review_by(dt.date(2028, 2, 29)) == dt.date(2128, 2, 28)
    assert tech_review_by(dt.date(2026, 1, 31)) == dt.date(2126, 1, 28)


def test_frontmatter_is_dropped_not_parsed():
    p = plan({"docs/a.md": "---\nid: [broken\n---\n# T\nbody\n", "docs/b.md": "---\n# U\nbody\n"})
    a, b = p.upserts
    assert (a.body, a.title) == ("# T\nbody\n", "T")
    assert b.body == "---\n# U\nbody\n" and b.title == "U"
    assert p.alerts == []


def test_nul_is_removed():
    [u] = plan({"docs/a.md": "# T\nx\x00y\n"}).upserts
    assert u.body == "# T\nxy\n"


def test_labels_come_from_config_not_the_file():
    [u] = plan({"docs/a.md": "---\nlabels: [other]\n---\n# T\n"}).upserts
    assert u.labels == ["ember"]
    assert (u.audience, u.tier, u.doc_key, u.supersedes, u.review_by) == (
        "all", TECH_TIER, "docs/a.md", [], dt.date(2126, 11, 18),
    )  # fmt: skip


def test_missing_known_label_is_left_off_with_one_alert():
    p = plan({"docs/a.md": "# T\n"}, labels=())
    assert p.upserts[0].labels == []
    assert p.alerts == [("", "label ember is not in kh.labels; run kh setup apply")]


def test_relabel_is_the_configured_known_labels():
    assert plan({}).relabel == ["ember"]
    assert plan({}, labels=()).relabel == []


def test_unreadable_text_skips_the_file_deletes_its_row_and_alerts():
    stored = [("docs/a.md", "old", "docs/a.md")]
    texts = {"docs/a.md": None, "docs/b.md": "# B"}
    p = plan({"docs/a.md": "x", "docs/b.md": "y"}, stored=stored, texts=texts)
    assert [u.path for u in p.upserts] == ["docs/b.md"]
    assert p.deletes == ["docs/a.md"]
    assert p.alerts == [("docs/a.md", "file skipped: not UTF-8")]


def test_deleted_and_excluded_files_are_deleted():
    stored = [("docs/gone.md", "s", "docs/gone.md"), ("src/a.md", "s", "src/a.md")]
    p = plan({"src/a.md": "x", "docs/a.md": "# A"}, stored=stored)
    assert p.deletes == ["docs/gone.md", "src/a.md"]
    assert [u.path for u in p.upserts] == ["docs/a.md"]


def test_unchanged_file_is_not_upserted():
    stored = [("docs/a.md", blob_sha("# A"), "docs/a.md")]
    p = plan({"docs/a.md": "# A"}, stored=stored)
    assert p.upserts == [] and p.deletes == []


def test_changed_text_missing_raises():
    with pytest.raises(ValueError, match="no text for changed file docs/a.md"):
        plan({"docs/a.md": "# A"}, texts={})
