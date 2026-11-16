from __future__ import annotations

import unicodedata
from datetime import UTC, date, datetime

import pytest

from knowledge_hub.agent import rules, texts
from knowledge_hub.auth.claims import Caller
from knowledge_hub.config import Config
from knowledge_hub.governance.rules import parse_layout
from knowledge_hub.mcp.errors import KhError

pytestmark = pytest.mark.unit

CONFIG = Config()
TODAY = date(2026, 11, 16)
UID = "0a1b2c3d-1111-2222-3333-444455556666"


def refused(code, fn, *args, **kwargs):
    with pytest.raises(KhError) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code
    return exc.value


def caller(**kw):
    base = dict(
        sub="s1",
        principal_type="claude_client",
        email="Alice@Example.com",
        audiences=frozenset({"all"}),
        token_id=None,
    )
    base.update(kw)
    return Caller(**base)


# requester
def test_requester_maps_email_to_lower_case_login():
    r = rules.requester_from_caller(caller(), ["example.com"])
    assert (r.sub, r.login, r.email) == ("s1", "alice", "Alice@Example.com")


@pytest.mark.parametrize("kw", [{"principal_type": "n8n_workflow"}, {"email": None}, {"email": ""}])
def test_requester_refuses_non_person(kw):
    assert (
        refused("person_only", rules.requester_from_caller, caller(**kw), ["example.com"]).text
        == texts.PERSON_ONLY
    )


@pytest.mark.parametrize(
    "email", ["a@other.com", "-a@example.com", "a b@example.com", "a" * 40 + "@example.com", "noat"]
)
def test_requester_refuses_unmappable_email(email):
    refused("bad_login", rules.requester_from_caller, caller(email=email), ["example.com"])


def test_requester_domain_compare_is_case_folded_and_empty_list_refuses():
    assert rules.requester_from_caller(caller(), ["EXAMPLE.com"]).login == "alice"
    refused("bad_login", rules.requester_from_caller, caller(), [])


def test_requester_accepts_39_character_login():
    assert (
        rules.requester_from_caller(caller(email="a" * 39 + "@example.com"), ["example.com"]).login
        == "a" * 39
    )


# ids
def test_check_id_accepts_and_refuses():
    assert rules.check_id("my-note-1") == "my-note-1"
    assert rules.check_id("a" * 80) == "a" * 80
    for bad in ("a" * 81, "", "A", "a--b", "-a", "a_b", "a\n", "a/b"):
        refused("bad_id_format", rules.check_id, bad)


# text hygiene
@pytest.mark.parametrize(
    "ch",
    [
        "​",  # zero-width space (Cf)
        "‮",  # right-to-left override (Cf)
        "­",  # soft hyphen (Cf)
        "\U000e0041",  # tag character
        "️",  # variation selector
        "\U000e0100",  # variation selector supplement
        "",  # private use (Co)
        " ",  # line separator (Zl)
        " ",  # paragraph separator (Zp)
        "͸",  # unassigned (Cn)
        "\ud800",  # surrogate (Cs)
        "\x00",
        "\x1b",
        "\x7f",
        "\r",
        "᠋",
        "᠏",
        "឴",
        "឵",
        "⠀",
        "͏",
        "ᅟ",
        "ᅠ",
        "ㅤ",
        "ﾠ",
    ],
)
def test_clean_text_refuses_hidden_characters(ch):
    refused("bad_text", rules.clean_text, f"a{ch}b", multiline=True)


def test_clean_text_refuses_tag_characters():
    payload = "".join(chr(0xE0000 + ord(c)) for c in "ignore")
    refused("bad_text", rules.clean_text, "hello" + payload, multiline=True)


def test_clean_text_refuses_html_comment():
    refused("bad_text", rules.clean_text, "a <!-- x --> b", multiline=True)


def test_clean_text_newline_rules():
    assert rules.clean_text("a\nb\tc", multiline=True) == "a\nb\tc"
    refused("bad_text", rules.clean_text, "a\nb", multiline=False)
    assert rules.clean_text("a\tb", multiline=False) == "a\tb"


def test_clean_text_accepts_accents_and_returns_nfc():
    assert rules.clean_text("árvíztűrő", multiline=False) == "árvíztűrő"
    decomposed = "é"
    out = rules.clean_text(decomposed, multiline=False)
    assert out == "é" and out == unicodedata.normalize("NFC", out)


# title
def test_check_title_strips_and_bounds():
    assert rules.check_title("  Hello  ") == "Hello"
    assert len(rules.check_title("a" * 200)) == 200
    for bad in ("", "   ", "a" * 201, "a\nb"):
        refused("bad_title" if "\n" not in bad else "bad_text", rules.check_title, bad)


@pytest.mark.parametrize("bad", ["a <b>", "<!x", "x <?php", "a </b", "<a"])
def test_check_title_refuses_markup(bad):
    err = refused_any(rules.check_title, bad)
    assert err.code in ("bad_title", "bad_text")


def refused_any(fn, *args):
    with pytest.raises(KhError) as exc:
        fn(*args)
    return exc.value


def test_check_title_allows_lone_angle_bracket_before_space_or_digit():
    assert rules.check_title("a < 3 and 2 <3") == "a < 3 and 2 <3"


# review_by
def test_check_review_by_window():
    assert rules.check_review_by("2026-11-17", TODAY) == date(2026, 11, 17)
    assert rules.check_review_by("2028-11-15", TODAY) == date(2028, 11, 15)
    for bad in ("2026-11-16", "2026-11-15", "2028-11-16", "tomorrow", "2026-13-01", ""):
        refused("bad_review_by", rules.check_review_by, bad, TODAY)


# entities
def test_check_entities_dedupes_in_order():
    assert rules.check_entities(["prd:a", "cmp:b", "prd:a"], CONFIG, "all") == ["prd:a", "cmp:b"]


@pytest.mark.parametrize(
    "bad", [["prd:A"], ["prd"], ["zzz:a"], ["prd:"], ["prd:a b"], ["prd:a\n"], ["cus:acme"]]
)
def test_check_entities_refuses(bad):
    refused("bad_entity", rules.check_entities, bad, CONFIG, "all")


def test_check_entities_count_limit_and_kind_audience():
    refused("bad_entity", rules.check_entities, [f"prd:p{i}" for i in range(21)], CONFIG, "all")
    assert len(rules.check_entities([f"prd:p{i}" for i in range(20)], CONFIG, "all")) == 20
    assert rules.check_entities(["cus:acme"], CONFIG, "contracts") == ["cus:acme"]


# markup (notes)
@pytest.mark.parametrize("bad", ["<b>x", "a </div>", "<!-x", "<?xml", "text\n[ref]: http://x", "  [a b]: y"])
def test_check_markup_refuses(bad):
    refused("bad_markup", rules.check_markup, bad)


@pytest.mark.parametrize("ok", ["a < b", "x <3", "[link](http://x)", "[ref]:", "1 < 2 > 0"])
def test_check_markup_accepts(ok):
    assert rules.check_markup(ok) == ok


# body
def test_check_body_normalises_like_promote():
    assert rules.check_body("\r\n\r\nline  \r\nnext\t\r\n\r\n", 100) == "line\nnext"


def test_check_body_size_in_bytes():
    assert rules.check_body("é" * 5, 10) == "é" * 5
    err = refused("bad_body", rules.check_body, "é" * 6, 10)
    assert err.text == texts.BAD_BODY.format(n=10)


@pytest.mark.parametrize("bad", ["", "  \n ", "---\nx", "\n\n---  \ntitle: x", "   ---\nx"])
def test_check_body_refuses_empty_and_frontmatter(bad):
    refused("bad_body", rules.check_body, bad, 1000)


def test_check_body_allows_rule_later_and_hygiene_applies():
    assert rules.check_body("intro\n---\nmore", 100) == "intro\n---\nmore"
    refused("bad_text", rules.check_body, "a‮b", 100)


# rationale
def test_check_rationale_bounds():
    assert rules.check_rationale("why") == "why"
    assert len(rules.check_rationale("a" * 2000)) == 2000
    refused("bad_rationale", rules.check_rationale, "")
    refused("bad_rationale", rules.check_rationale, "a" * 2001)
    refused("bad_text", rules.check_rationale, "a\x00")


# base commit
def test_check_commit_formats():
    assert rules.check_commit("") == ""
    assert rules.check_commit("a" * 40) == "a" * 40
    assert rules.check_commit("0" * 64) == "0" * 64
    for bad in ("a" * 39, "A" * 40, "a" * 41, "g" * 40, "a" * 40 + "\n"):
        refused("bad_commit", rules.check_commit, bad)


# paths
def test_note_path():
    assert rules.note_path("my-note") == "agent/my-note.md"


def test_doc_path_builds_with_and_without_component():
    path, layout = rules.doc_path(CONFIG, "product", "app", "", "specs", "my-spec")
    assert path == "product/app/specs/my-spec.md" and layout.component is None
    path, layout = rules.doc_path(CONFIG, "product", "app", "ui", "guides", "my-guide")
    assert path == "product/app/ui/guides/my-guide.md" and layout.component == "ui"


def test_doc_path_matches_parse_layout_for_component_folder():
    path, layout = rules.doc_path(CONFIG, "technical", "app", "core", "requirements", "r-1")
    assert parse_layout(path, CONFIG.domains) == layout


@pytest.mark.parametrize("kind", ["adr", "glossary", "", "Specs", "specs/"])
def test_doc_path_refuses_kind(kind):
    refused("bad_kind", rules.doc_path, CONFIG, "product", "app", "", kind, "x")


@pytest.mark.parametrize(
    ("product", "component"),
    [
        ("..", ""),
        ("a//b", ""),
        (".forgejo", ""),
        ("a/b", ""),
        ("App", ""),
        ("", ""),
        ("app", ".."),
        ("app", "a/b"),
        ("app", "A"),
    ],
)
def test_doc_path_refuses_segments(product, component):
    refused("bad_segment", rules.doc_path, CONFIG, "product", product, component, "specs", "x")


def test_doc_path_refuses_component_named_like_a_kind_and_unknown_domain():
    refused("bad_path", rules.doc_path, CONFIG, "product", "app", "specs", "guides", "x")
    refused("bad_path", rules.doc_path, CONFIG, "hr", "app", "", "specs", "x")
    refused("bad_path", rules.doc_path, CONFIG, ".forgejo", "app", "", "specs", "x")


@pytest.mark.parametrize("bad_id", ["", "a/b", "../x", "A", "a\n", "a//b", "glossary/x"])
def test_doc_path_refuses_bad_id(bad_id):
    refused("bad_path", rules.doc_path, CONFIG, "product", "app", "", "specs", bad_id)


# citations
def test_cited_ids_extracts_lowers_sorts_dedupes():
    upper = UID.upper()
    other = "ffffffff-1111-2222-3333-444455556666"
    got = rules.cited_ids(f"see DOC:{upper} and derived:{other}", f"doc:{UID}, raw:{UID}.")
    assert got == sorted({f"doc:{UID}", f"derived:{other}", f"raw:{UID}"})


def test_cited_ids_ignores_near_misses():
    assert rules.cited_ids(f"doc:{UID}x", f"xdoc:{UID}", "doc:1234", f"foo:{UID}", f"doc:{UID[:-1]}") == []


# day
def test_day_start_and_next_day_text():
    now = datetime(2026, 11, 16, 13, 45, 12, tzinfo=UTC)
    assert rules.day_start(now) == datetime(2026, 11, 16, tzinfo=UTC)
    assert rules.next_day_text(now) == "2026-11-17 00:00"
    assert rules.next_day_text(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == "2027-01-01 00:00"
