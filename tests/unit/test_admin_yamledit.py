"""Round-trip editor for kh.yaml (plan 45, 3.4)."""

import fcntl
import os
import stat
from pathlib import Path

import pytest
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from knowledge_hub.admin import yamledit
from knowledge_hub.config import ConfigError

pytestmark = pytest.mark.unit

EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / "kh.example.yaml"


def _example(tmp_path: Path) -> tuple[Path, yamledit.Doc]:
    path = tmp_path / "kh.yaml"
    path.write_text(EXAMPLE.read_text())
    return path, yamledit.load(path)


def _changed(diff: str) -> tuple[list[str], list[str]]:
    removed = [line[1:] for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")]
    added = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    return removed, added


def test_roundtrip_of_example_file_is_identical(tmp_path):
    _, doc = _example(tmp_path)
    assert yamledit.render(doc) == doc.text
    assert yamledit.unified_diff(doc.text, yamledit.render(doc), "kh.yaml") == ""


def test_validate_rejects_invalid_text_with_config_prefix():
    with pytest.raises(ConfigError, match=r"^config: "):
        yamledit.validate("groups: {all: {members: [a], approvers: [b]}}\n")
    with pytest.raises(ConfigError, match=r"^config: "):
        yamledit.validate("- a\n- b\n")
    assert yamledit.validate(EXAMPLE.read_text()).org == "kh"


def test_commit_replaces_atomically_and_keeps_mode(tmp_path):
    path = tmp_path / "kh.yaml"
    path.write_text("org: a\n")
    path.chmod(0o640)
    inode = path.stat().st_ino
    yamledit.commit(path, "org: b\n")
    assert path.read_text() == "org: b\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.stat().st_ino != inode
    assert [p.name for p in tmp_path.iterdir()] == ["kh.yaml"]


def test_commit_follows_a_symlink(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "kh.yaml"
    real.write_text("org: a\n")
    link = tmp_path / "kh.yaml"
    link.symlink_to(real)
    yamledit.commit(link, "org: b\n")
    assert link.is_symlink()
    assert real.read_text() == "org: b\n"


def test_commit_reports_unwritable_directory(tmp_path):
    missing = tmp_path / "nope" / "kh.yaml"
    with pytest.raises(ConfigError, match=r"^admin: cannot write .*kh\.yaml: "):
        yamledit.commit(missing, "org: b\n")


def test_locked_blocks_a_second_holder(tmp_path):
    path = tmp_path / "kh.yaml"
    path.write_text("org: a\n")
    with yamledit.locked(path):
        fd = os.open(tmp_path / ".kh.yaml.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        with pytest.raises(ConfigError, match=r"^admin: cannot lock "), yamledit.locked(path):
            pass
    with yamledit.locked(path):
        pass


def test_load_empty_file_gives_empty_mapping(tmp_path):
    path = tmp_path / "kh.yaml"
    path.write_text("")
    doc = yamledit.load(path)
    assert isinstance(doc.data, CommentedMap)
    assert len(doc.data) == 0
    path.write_text("# only a comment\n")
    assert len(yamledit.load(path).data) == 0


def test_load_rejects_non_mapping_and_unreadable(tmp_path):
    path = tmp_path / "kh.yaml"
    path.write_text("- a\n")
    with pytest.raises(ConfigError, match=r"^config: top level must be a mapping$"):
        yamledit.load(path)
    with pytest.raises(ConfigError, match=r"^config: cannot read "):
        yamledit.load(tmp_path / "missing.yaml")


def _flow(*items: str) -> CommentedSeq:
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def test_golden_add_label_changes_only_its_line(tmp_path):
    _, doc = _example(tmp_path)
    doc.data["labels"]["topic:finance"] = "Finance"
    new = yamledit.render(doc)
    removed, added = _changed(yamledit.unified_diff(doc.text, new))
    assert removed == []
    assert added == ["  topic:finance: Finance"]
    yamledit.validate(new)


def test_golden_remove_label_keeps_other_comments(tmp_path):
    _, doc = _example(tmp_path)
    del doc.data["labels"]["product:ember"]
    new = yamledit.render(doc)
    removed, added = _changed(yamledit.unified_diff(doc.text, new))
    assert removed == ["  product:ember: Ember product"]
    assert added == []
    assert "# Technical repos (tier T0a" in new
    assert "#   ember-core:" in new
    assert "# archived_labels:" in new


def test_golden_add_group_appends_a_block(tmp_path):
    _, doc = _example(tmp_path)
    group = CommentedMap()
    group["members"] = _flow("alice")
    group["approvers"] = _flow("alice")
    doc.data["groups"]["eng"] = group
    new = yamledit.render(doc)
    removed, added = _changed(yamledit.unified_diff(doc.text, new))
    assert removed == []
    assert added == ["  eng:", "    members: [alice]", "    approvers: [alice]"]
    assert yamledit.validate(new).groups["eng"].approvers == ["alice"]


def test_golden_add_person_adds_one_line(tmp_path):
    _, doc = _example(tmp_path)
    doc.data["people"]["carol"] = "carol@example.com"
    new = yamledit.render(doc)
    removed, added = _changed(yamledit.unified_diff(doc.text, new))
    assert removed == []
    assert added == ["  carol: carol@example.com"]


def test_golden_set_archived_on_group(tmp_path):
    _, doc = _example(tmp_path)
    doc.data["groups"]["mgmt"]["archived"] = True
    new = yamledit.render(doc)
    removed, added = _changed(yamledit.unified_diff(doc.text, new))
    assert removed == []
    assert added == ["    archived: true"]
    assert yamledit.validate(new).groups["mgmt"].archived is True
