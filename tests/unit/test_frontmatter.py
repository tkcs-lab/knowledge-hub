import datetime as dt

import pytest

from knowledge_hub.indexer.frontmatter import ParsedDoc, Rejected, parse_document


def doc(fm: str, body: str = "# Hi\nbody\n") -> str:
    return f"---\n{fm}---\n{body}"


GOOD = "id: doc-1\ntitle: Gateway\nreview_by: 2027-01-31\n"


def reason(text: str) -> str:
    r = parse_document(text, "p.md")
    assert isinstance(r, Rejected)
    return r.reason


def test_happy_path():
    r = parse_document(doc(GOOD + "labels: [product:ember, a.b]\nowner: x\nextra: [1]\n"), "p.md")
    assert r == ParsedDoc(
        id="doc-1",
        title="Gateway",
        review_by=dt.date(2027, 1, 31),
        labels=["product:ember", "a.b"],
        body="# Hi\nbody\n",
    )


def test_labels_optional_and_null():
    assert parse_document(doc(GOOD), "p.md").labels == []
    assert parse_document(doc(GOOD + "labels:\n"), "p.md").labels == []


def test_quoted_date_and_bom():
    r = parse_document("﻿" + doc('id: a\ntitle: T\nreview_by: "2027-02-03"\n'), "p.md")
    assert isinstance(r, ParsedDoc) and r.review_by == dt.date(2027, 2, 3)


def test_title_is_stripped():
    assert parse_document(doc("id: a\ntitle: '  T  '\nreview_by: 2027-01-01\n"), "p.md").title == "T"


@pytest.mark.parametrize("text", ["# no frontmatter\n", "---\nid: a\n", "", "---\n"])
def test_missing_frontmatter(text):
    assert reason(text) == "missing frontmatter"


@pytest.mark.parametrize("fm", ["id: [unclosed\n", "- a\n- b\n", "just text\n", ""])
def test_invalid_frontmatter(fm):
    assert reason(doc(fm)) == "invalid frontmatter"


@pytest.mark.parametrize("key", ["id", "title", "review_by"])
def test_missing_key(key):
    lines = [ln for ln in GOOD.splitlines(keepends=True) if not ln.startswith(key + ":")]
    assert reason(doc("".join(lines))) == f"missing {key}"
    assert reason(doc("".join(lines) + f"{key}:\n")) == f"missing {key}"


@pytest.mark.parametrize("bad", ["Doc", "-a", "a b", "a/b", "x" * 101, "42", "''", "[a]"])
def test_invalid_id(bad):
    assert reason(doc(f"id: {bad}\ntitle: T\nreview_by: 2027-01-01\n")) == "invalid id"


def test_id_limits():
    text = doc("id: " + "a" * 100 + "\ntitle: T\nreview_by: 2027-01-01\n")
    assert isinstance(parse_document(text, "p"), ParsedDoc)


@pytest.mark.parametrize("bad", ["''", "'   '", "[a]", "5"])
def test_invalid_title(bad):
    assert reason(doc(f"id: a\ntitle: {bad}\nreview_by: 2027-01-01\n")) == "invalid title"


@pytest.mark.parametrize(
    "bad", ["tomorrow", "2027-13-01", "2027-02-30", "2027-1-1", "2027-01-01T10:00:00", "20270101", "[x]"]
)
def test_invalid_review_by(bad):
    assert reason(doc(f"id: a\ntitle: T\nreview_by: {bad}\n")) == "invalid review_by"


@pytest.mark.parametrize("bad", ["x", "[A]", "[a, 1]", "[a, [b]]", "{a: b}", "['']", "[a b]"])
def test_invalid_labels(bad):
    assert reason(doc(GOOD + f"labels: {bad}\n")) == "invalid labels"
