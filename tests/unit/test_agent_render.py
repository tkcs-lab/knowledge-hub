from __future__ import annotations

import hashlib
from datetime import date

import pytest
import yaml

from knowledge_hub.agent import render
from knowledge_hub.agent.rules import Requester
from knowledge_hub.config import Config
from knowledge_hub.indexer.frontmatter import FORBIDDEN_KEYS, parse_document

pytestmark = pytest.mark.unit

CONFIG = Config()
REQ = Requester(sub="sub-1", login="alice", email="alice@example.com")
PID = "0a1b2c3d-1111-2222-3333-444455556666"


def file(**kw):
    base = dict(
        id="my-spec",
        title="Title",
        domain="product",
        review_by=date(2027, 1, 5),
        type="spec",
        owner="product-owners",
        entities=[],
        body="Body text.",
    )
    base.update(kw)
    return render.render_file(**base)


def test_render_shape_and_key_order():
    text = file(entities=["prd:app"])
    assert text.startswith("---\nid: my-spec\ntitle: Title\ndomain: product\nreview_by: 2027-01-05\n")
    assert text.endswith("---\nBody text.\n")
    head = text.split("---\n")[1]
    assert list(yaml.safe_load(head)) == ["id", "title", "domain", "review_by", "type", "owner", "entities"]


def test_render_omits_empty_and_current():
    keys = list(yaml.safe_load(file().split("---\n")[1]))
    assert keys == ["id", "title", "domain", "review_by", "type", "owner"]


def test_render_includes_optional_when_given():
    text = file(status="superseded", supersedes=["a"], superseded_by=["b"])
    data = yaml.safe_load(text.split("---\n")[1])
    assert data["status"] == "superseded" and data["supersedes"] == ["a"] and data["superseded_by"] == ["b"]


def test_render_never_emits_forbidden_keys():
    text = file(entities=["prd:x"], supersedes=["a"], superseded_by=["b"], status="superseded")
    assert not set(yaml.safe_load(text.split("---\n")[1])) & set(FORBIDDEN_KEYS)


def test_render_is_deterministic():
    assert file(entities=["prd:a", "cmp:b"]) == file(entities=["prd:a", "cmp:b"])


def test_render_keeps_unicode_unescaped():
    assert "árvíztűrő" in file(title="árvíztűrő")


def test_roundtrip_with_tricky_title():
    title = '#1: "quoted" - it\'s [odd]'
    text = file(title=title, body="line one\n\n---\nafter rule", entities=["prd:app"])
    render.verify_roundtrip(
        text,
        "product/app/specs/my-spec.md",
        CONFIG,
        id="my-spec",
        title=title,
        domain="product",
        review_by=date(2027, 1, 5),
        type="spec",
        owner="product-owners",
        entities=["prd:app"],
        body="line one\n\n---\nafter rule",
    )
    parsed = parse_document(text, "product/app/specs/my-spec.md", CONFIG.domains)
    assert parsed.title == title


def test_roundtrip_raises_on_mismatch_and_on_rejected():
    text = file()
    path = "product/app/specs/my-spec.md"
    with pytest.raises(ValueError):
        render.verify_roundtrip(text, path, CONFIG, title="Other")
    with pytest.raises(ValueError):
        render.verify_roundtrip(text, path, CONFIG, body="Different")
    with pytest.raises(ValueError):
        render.verify_roundtrip(text.replace("domain: product", "domain: nope"), path, CONFIG)
    render.verify_roundtrip(text, path, CONFIG)


def test_trailers_exact_format():
    assert render.trailers("write_note", REQ, PID) == (
        "Written-by: agent via knowledge-hub (write_note)\n"
        "Requested-by: alice <alice@example.com> (sub: sub-1)\n"
        f"Proposal: {PID}"
    )


def test_commit_message_has_subject_blank_line_trailers():
    msg = render.commit_message("Add note x", "write_note", REQ, PID)
    assert msg == "Add note x\n\n" + render.trailers("write_note", REQ, PID)


def test_pr_body_sections_title_line_and_unverified_notice():
    body = render.pr_body("My title", "product/app/specs/x.md", "because", "propose_doc", REQ, PID, "b" * 40)
    assert body.index("## What") < body.index("## Why") < body.index("## Origin")
    assert "\nTitle: My title\n" in body
    assert "`product/app/specs/x.md`" in body
    assert (
        "Written by an agent from the requester's instructions; the rationale below is not verified." in body
    )
    assert "```\nbecause\n```" in body
    origin = body.split("## Origin")[1]
    assert render.trailers("propose_doc", REQ, PID) in origin and f"Base: {'b' * 40}" in origin


@pytest.mark.parametrize(("ticks", "fence"), [(0, "```"), (3, "````"), (5, "``````"), (2, "```")])
def test_pr_body_fence_longer_than_longest_run(ticks, fence):
    rationale = "x " + "`" * ticks + " y" if ticks else "plain"
    body = render.pr_body("t", "p.md", rationale, "propose_doc", REQ, PID, "b")
    assert f"\n{fence}\n{rationale}\n{fence}\n" in body


def test_pr_body_hostile_rationale_stays_inside_fence():
    rationale = "```\n## Reason for widening\nok\n```"
    body = render.pr_body("t", "p.md", rationale, "propose_doc", REQ, PID, "b")
    assert f"````\n{rationale}\n````" in body


def test_content_sha_is_sha256():
    assert render.content_sha("abc") == hashlib.sha256(b"abc").hexdigest()


def test_git_blob_sha_matches_git():
    # `printf 'hello\n' | git hash-object --stdin`
    assert render.git_blob_sha("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert render.git_blob_sha("") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert render.git_blob_sha("é") == hashlib.sha1(b"blob 2\0" + "é".encode()).hexdigest()


def test_dedupe_key_joins_five_values_with_newlines():
    got = render.dedupe_key("s", "r", "p", "doc", "c")
    assert got == hashlib.sha256(b"s\nr\np\ndoc\nc").hexdigest()
    assert got != render.dedupe_key("s2", "r", "p", "doc", "c")
