"""propose_render: path and text rules, and the rendered file."""

from __future__ import annotations

import pytest

from knowledge_hub.indexer.frontmatter import ParsedDoc, parse_document
from knowledge_hub.propose_render import check_text, render_file, validate_path


@pytest.mark.parametrize("path", ["a.md", "docs/a-b_c.md", "0/x/y.md", "a/b/c/d.md"])
def test_good_paths(path):
    assert validate_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "README.md",
        "A.md",
        "a.txt",
        "a",
        "/a.md",
        "a//b.md",
        "../a.md",
        "a/../b.md",
        ".a.md",
        "a/.b.md",
        "a/.md",
        "a/b/",
        "-a.md",
        "a b.md",
        "a.md\n",
        "a/b..md",
    ],
)
def test_bad_paths(path):
    assert not validate_path(path)


@pytest.mark.parametrize("text", ["", "plain", "tab\tand\nnewline", "a < b and c > d", "1<2", "ünï 日本"])
def test_good_text(text):
    assert check_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "a\x00b",
        "a\rb",
        "a\x1bb",
        "a\x7fb",
        "a\x85b",
        "a‮b",
        "a‪b",
        "a⁦b",
        "a⁩b",
        "x <!-- c --> y",
        "<!--",
        "<div>",
        "a <b>bold</b>",
        "</p>",
        "<script src=x>",
    ],
)
def test_bad_text(text):
    assert not check_text(text)


def test_render_layout():
    out = render_file("my-id", 'T "q"', "2027-01-31", ["a", "b"], "Body\n")
    assert out == (
        '---\nid: "my-id"\ntitle: "T \\"q\\""\nreview_by: "2027-01-31"\nlabels: ["a", "b"]\n---\nBody\n'
    )


def test_render_parses_back():
    body = "line one\n---\n# H\n\ntext: with colon\n"
    out = render_file("d1", "A: tricky # title", "2027-01-31", ["x"], body)
    doc = parse_document(out, "d1.md")
    assert isinstance(doc, ParsedDoc)
    assert (doc.id, doc.title, doc.review_by.isoformat(), doc.labels, doc.body) == (
        "d1",
        "A: tricky # title",
        "2027-01-31",
        ["x"],
        body,
    )


def test_render_empty_labels():
    doc = parse_document(render_file("d1", "T", "2027-01-31", [], "b"), "d1.md")
    assert isinstance(doc, ParsedDoc) and doc.labels == []


def test_render_writes_supersedes_line_only_when_given():
    assert "supersedes" not in render_file("d1", "T", "2027-01-31", ["x"], "b")
    assert "supersedes" not in render_file("d1", "T", "2027-01-31", ["x"], "b", [])
    out = render_file("d1", "T", "2027-01-31", ["x"], "b", ["a", "b"])
    assert out == (
        '---\nid: "d1"\ntitle: "T"\nreview_by: "2027-01-31"\nlabels: ["x"]\nsupersedes: ["a", "b"]\n---\nb'
    )


def test_rendered_supersedes_round_trips_through_parse_document():
    doc = parse_document(render_file("d1", "T", "2027-01-31", [], "b", ["a", "b-2"]), "d1.md")
    assert isinstance(doc, ParsedDoc) and doc.supersedes == ["a", "b-2"]
