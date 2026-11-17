"""``kh lint FILE...``: offline frontmatter check."""

import pytest

from knowledge_hub import cli

pytestmark = pytest.mark.unit

GOOD = "---\nid: good\ntitle: Good\nreview_by: 2099-01-01\n---\nBody\n"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith("KH_"):
            monkeypatch.delenv(k)


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_good_file_passes(tmp_path, capsys):
    assert cli.main(["lint", write(tmp_path, "good.md", GOOD)]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("no frontmatter\n", "missing frontmatter"),
        ("---\nid: [unclosed\n---\nx\n", "invalid frontmatter"),
        ("---\n- a\n- b\n---\nx\n", "invalid frontmatter"),
        ("---\ntitle: T\nreview_by: 2099-01-01\n---\nx\n", "missing id"),
        ("---\nid: a\nreview_by: 2099-01-01\n---\nx\n", "missing title"),
        ("---\nid: a\ntitle: T\n---\nx\n", "missing review_by"),
        ("---\nid: a\ntitle: T\nreview_by: 2099-13-45\n---\nx\n", "invalid review_by"),
        ("---\nid: BAD ID\ntitle: T\nreview_by: 2099-01-01\n---\nx\n", "invalid id"),
    ],
)
def test_each_bad_file_prints_its_reason(tmp_path, capsys, text, reason):
    path = write(tmp_path, "bad.md", text)
    assert cli.main(["lint", path]) == 1
    assert capsys.readouterr().out == f"{path}: {reason}\n"


def test_missing_file_is_a_problem(tmp_path, capsys):
    path = str(tmp_path / "nope.md")
    assert cli.main(["lint", path]) == 1
    assert capsys.readouterr().out.startswith(f"{path}: cannot read file")


def test_every_file_is_checked_and_one_problem_fails(tmp_path, capsys):
    good = write(tmp_path, "good.md", GOOD)
    bad = write(tmp_path, "bad.md", "nothing\n")
    assert cli.main(["lint", good, bad, good]) == 1
    assert capsys.readouterr().out == f"{bad}: missing frontmatter\n"


def test_lint_needs_a_file_and_no_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("KH_ENV", "prod")
    monkeypatch.setenv("KH_EMBED_PROVIDER", "fake")  # refused settings must not matter to lint
    assert cli.main(["lint", write(tmp_path, "good.md", GOOD)]) == 0
    with pytest.raises(SystemExit) as exc:
        cli.main(["lint"])
    assert exc.value.code == 2
