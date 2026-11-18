import random

import pytest

from knowledge_hub.indexer.supersede import DocLink, resolve

pytestmark = pytest.mark.unit


def row(key, supersedes=(), current=None):
    path = f"{key}.md"
    return DocLink(id=f"id-{key}", doc_key=key, path=path, supersedes=tuple(supersedes), current=current)


def test_unknown_id_alerts():
    r = resolve([row("a", ["ghost"])])
    assert r.alerts == [("a.md", "supersedes unknown id ghost")]
    assert r.links == {"id-a": None}


def test_self_is_skipped():
    r = resolve([row("a", ["a"])])
    assert r.links == {"id-a": None} and r.alerts == []


def test_two_successors_pick_the_smallest_doc_key():
    r = resolve([row("old"), row("z", ["old"]), row("m", ["old"])])
    assert r.links["id-old"] == "id-m"
    assert r.alerts == [("old.md", "superseded by 2 documents; using m")]


def test_two_document_cycle():
    r = resolve([row("a", ["b"]), row("b", ["a"])])
    assert r.links == {"id-a": None, "id-b": None}
    assert r.alerts == [("a.md", "supersedes cycle: a, b"), ("b.md", "supersedes cycle: a, b")]


def test_three_document_cycle():
    r = resolve([row("a", ["b"]), row("b", ["c"]), row("c", ["a"])])
    assert set(r.links.values()) == {None}
    assert r.alerts == [(f"{k}.md", "supersedes cycle: a, b, c") for k in "abc"]


def test_chain_keeps_direct_links():
    r = resolve([row("a"), row("b", ["a"]), row("c", ["b"])])
    assert r.links == {"id-a": "id-b", "id-b": "id-c", "id-c": None}
    assert r.alerts == []


def test_every_input_row_is_in_links():
    rows = [row("a"), row("b", ["a"]), row("c", ["nope"])]
    assert set(resolve(rows).links) == {r.id for r in rows}


def test_resolve_is_deterministic_for_shuffled_input():
    rows = [row("a", ["b"]), row("b", ["a"]), row("o"), row("x", ["o"]), row("y", ["o", "q"])]
    rows.append(row("c", ["a"]))
    want = resolve(rows)
    for seed in range(10):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert resolve(shuffled) == want
