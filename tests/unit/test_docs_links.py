"""Docs hygiene: relative markdown links resolve, and markdown files stay short."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SKIP_DIRS = {".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}


def _doc_files() -> list[Path]:
    return [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def _links(text: str) -> list[str]:
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.extend(LINK.findall(re.sub(r"`[^`]*`", "", line)))
    return out


def test_relative_links_resolve() -> None:
    missing = []
    for doc in _doc_files():
        for target in _links(doc.read_text(encoding="utf-8")):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            if path and not (doc.parent / path).exists():
                missing.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert not missing, missing


def test_docs_are_found() -> None:
    names = {p.name for p in _doc_files()}
    assert {"README.md", "design.md", "configuration.md", "runbook.md"} <= names


def test_markdown_files_have_at_most_400_lines() -> None:
    long = []
    for path in ROOT.rglob("*.md"):
        rel = path.relative_to(ROOT)
        if SKIP_DIRS & set(rel.parts) or rel.parts[:2] == ("docs", "plans"):
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > 400:
            long.append(f"{rel}: {lines}")
    assert not long, long
