"""Pure rules of ``propose_doc`` (plan 3.8): path, text and the rendered file."""

from __future__ import annotations

import json
import re
import unicodedata

_PATH_RE = re.compile(r"[a-z0-9][a-z0-9/_-]*\.md")
_BIDI = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}
_TAG_RE = re.compile(r"</?[A-Za-z]")


def validate_path(path: str) -> bool:
    """A Markdown path the indexer takes: no dot segment, no ``//``, not the root ``README.md``."""
    if not _PATH_RE.fullmatch(path) or path == "README.md":
        return False
    if ".." in path or "//" in path:
        return False
    return all(seg and not seg.startswith(".") for seg in path.split("/"))


def check_text(text: str) -> bool:
    """True when ``text`` holds no control character (tab and newline apart), bidi control, comment or tag."""
    for ch in text:
        if ch in _BIDI:
            return False
        if ch not in "\t\n" and unicodedata.category(ch) == "Cc":
            return False
    return "<!--" not in text and _TAG_RE.search(text) is None


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_file(doc_id: str, title: str, review_by: str, labels: list[str], body: str) -> str:
    flow = ", ".join(_q(label) for label in labels)
    head = f"id: {_q(doc_id)}\ntitle: {_q(title)}\nreview_by: {_q(review_by)}\nlabels: [{flow}]"
    return f"---\n{head}\n---\n{body}"
