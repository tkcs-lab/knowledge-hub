import hashlib

from knowledge_hub.indexer.chunker import chunk_markdown


def test_empty():
    assert chunk_markdown("", "v1") == []
    assert chunk_markdown("# Only heading\n\n", "v1") == []


def test_nested_headings():
    body = "# A\n" + "a text " * 40 + "\n## B\n" + "b text " * 40 + "\n### C\n" + "c text " * 40
    cs = chunk_markdown(body, "v1")
    assert [c.heading_path for c in cs] == ["A", "A > B", "A > B > C"]
    assert [c.ordinal for c in cs] == [0, 1, 2]


def test_sibling_resets_path():
    body = "# A\n## B\n" + "x " * 150 + "\n## C\n" + "y " * 150
    assert [c.heading_path for c in chunk_markdown(body, "v")] == ["A > B", "A > C"]


def test_preamble_and_fence():
    body = "intro " * 50 + "\n# H\n```\n# not heading\n```\n" + "z " * 120
    cs = chunk_markdown(body, "v")
    assert [c.heading_path for c in cs] == ["", "H"]
    assert "# not heading" in cs[1].text


def test_long_section():
    para = "word " * 50  # ~250 chars
    body = "# L\n" + "\n\n".join(para.strip() + str(i) for i in range(20))
    cs = chunk_markdown(body, "v")
    assert len(cs) > 1
    assert all(len(c.text) <= 1200 + 150 + 2 for c in cs)
    assert all(c.heading_path == "L" for c in cs)
    # overlap: the tail of piece n starts piece n+1
    assert cs[1].text.startswith(cs[0].text[-150:].lstrip())


def test_single_giant_paragraph():
    cs = chunk_markdown("# G\n" + "abcd " * 1000, "v")
    assert len(cs) > 3 and all(len(c.text) <= 1352 for c in cs)


def test_tiny_trailing_piece_merges_in_section():
    body = "# S\n" + ("a" * 1000) + "\n\n" + ("b" * 150)
    cs = chunk_markdown(body, "v")
    assert len(cs) == 1 and cs[0].text.endswith("b" * 150)


def test_tiny_section_stays_separate():
    body = "# S\n" + "a" * 500 + "\n# T\ntiny"
    cs = chunk_markdown(body, "v")
    assert [c.heading_path for c in cs] == ["S", "T"] and cs[1].text == "tiny"


def test_sha_stable_and_versioned():
    body = "# A\nhello world"
    a, b = chunk_markdown(body, "v1")[0], chunk_markdown(body, "v1")[0]
    assert a.content_sha == b.content_sha
    assert a.content_sha == hashlib.sha256(b"v1|A|hello world").hexdigest()
    assert chunk_markdown(body, "v2")[0].content_sha != a.content_sha
