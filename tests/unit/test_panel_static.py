"""The static shell ships in the package and stays free of unsafe DOM and script patterns."""

from __future__ import annotations

import re
from importlib import resources

import pytest

pytestmark = pytest.mark.unit

FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write")


def _files():
    root = resources.files("knowledge_hub.panel").joinpath("static")
    return [f for f in root.iterdir() if f.is_file()]


def test_static_files_present_and_readable():
    names = {f.name for f in _files()}
    assert {"index.html", "panel.css", "app.js"} <= names
    for f in _files():
        assert f.read_bytes()


def test_scripts_free_of_unsafe_patterns():
    for f in _files():
        if f.name.endswith((".js", ".html")):
            text = f.read_text(encoding="utf-8")
            for bad in FORBIDDEN:
                assert bad not in text, f"{f.name}: {bad}"


def test_index_has_no_inline_code_or_style():
    text = resources.files("knowledge_hub.panel").joinpath("static/index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", text, re.I)
    assert not re.search(r"\sstyle\s*=", text, re.I)
    assert not re.search(r"<style", text, re.I)
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, re.I | re.S):
        assert m.group(1).strip() == ""
