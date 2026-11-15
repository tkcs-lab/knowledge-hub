import datetime as dt

import pytest

from knowledge_hub.indexer.frontmatter import ParsedDoc, Rejected, parse_document

DOMAINS = ["portfolio", "product", "technical"]
GOOD = "---\nid: doc-1\ntitle: Gateway\ndomain: product\nreview_by: 2027-01-31\n{extra}---\n# Hi\nbody\n"


def doc(extra: str = "") -> str:
    return GOOD.format(extra=extra)


def test_happy_path():
    r = parse_document(
        doc("owner: a@b.c\ntype: spec\nentities: [prd:gateway]\nsupersedes: old\nstatus: current\n"),
        "p.md",
        DOMAINS,
    )
    assert isinstance(r, ParsedDoc)
    assert r.id == "doc-1" and r.review_by == dt.date(2027, 1, 31)
    assert r.entities == ["prd:gateway"] and r.supersedes == ["old"] and r.superseded_by == []
    assert r.status == "current" and r.body.startswith("# Hi")


def test_superseded_status_ok():
    r = parse_document(doc("status: superseded\n"), "p.md", DOMAINS)
    assert isinstance(r, ParsedDoc) and r.status == "superseded"


@pytest.mark.parametrize(
    "key",
    ["tier", "audience", "audiences", "required_audiences", "allowed_emails", "visibility",
     "approved_by", "approved_at"],
)
def test_forbidden_keys(key):
    r = parse_document(doc(f"{key}: x\n"), "p.md", DOMAINS)
    assert r == Rejected(f"frontmatter must not set {key}; the server decides tier and audience")


def test_disputed():
    r = parse_document(doc("status: disputed\n"), "p.md", DOMAINS)
    assert r == Rejected("frontmatter: status disputed cannot be set by a file")


def test_bad_status():
    assert isinstance(parse_document(doc("status: weird\n"), "p.md", DOMAINS), Rejected)


@pytest.mark.parametrize("key", ["id", "title", "domain", "review_by"])
def test_missing_required(key):
    lines = [ln for ln in doc().splitlines(keepends=True) if not ln.startswith(key + ":")]
    r = parse_document("".join(lines), "p.md", DOMAINS)
    assert isinstance(r, Rejected) and r.reason.startswith("frontmatter: ") and key in r.reason


@pytest.mark.parametrize("val", ["not-a-date", "2027-13-45", "[1]"])
def test_bad_date(val):
    text = doc().replace("2027-01-31", val)
    r = parse_document(text, "p.md", DOMAINS)
    assert isinstance(r, Rejected) and r.reason.startswith("frontmatter: ")


def test_unknown_domain():
    r = parse_document(doc().replace("product", "sales"), "p.md", DOMAINS)
    assert isinstance(r, Rejected) and "domain" in r.reason


@pytest.mark.parametrize("text", ["no frontmatter", "---\nid: x\n", "---\n: : [\n---\nb", "---\n- a\n---\nb"])
def test_malformed(text):
    r = parse_document(text, "p.md", DOMAINS)
    assert isinstance(r, Rejected) and r.reason.startswith("frontmatter: ")
