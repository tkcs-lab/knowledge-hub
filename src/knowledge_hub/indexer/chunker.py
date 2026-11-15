"""Heading-based markdown chunker (plan section 3.6)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

MAX_CHARS = 1200
OVERLAP = 150
MIN_CHARS = 200

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading_path: str
    text: str
    content_sha: str


def _sections(body: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    path = ""
    in_fence = False

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            sections.append((path, text))
        buf.clear()

    for line in body.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            path = " > ".join(t for _, t in stack)
        else:
            buf.append(line)
    flush()
    return sections


def _pack(text: str) -> list[str]:
    """Split at paragraph boundaries into pieces of at most MAX_CHARS (no overlap yet)."""
    paras: list[str] = []
    for p in re.split(r"\n\s*\n", text):
        p = p.strip()
        while len(p) > MAX_CHARS:  # one paragraph too long: hard split
            cut = p.rfind(" ", 0, MAX_CHARS)
            cut = cut if cut > 0 else MAX_CHARS
            paras.append(p[:cut].rstrip())
            p = p[cut:].lstrip()
        if p:
            paras.append(p)
    pieces: list[str] = []
    cur = ""
    for p in paras:
        if cur and len(cur) + 2 + len(p) > MAX_CHARS:
            pieces.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        pieces.append(cur)
    # a small piece merges into the previous piece of the same section (when it still fits)
    merged: list[str] = []
    for p in pieces:
        if merged and len(p) < MIN_CHARS and len(merged[-1]) + 2 + len(p) <= MAX_CHARS:
            merged[-1] = f"{merged[-1]}\n\n{p}"
        else:
            merged.append(p)
    return merged


def _sha(version: str, heading_path: str, text: str) -> str:
    return hashlib.sha256(f"{version}|{heading_path}|{text}".encode()).hexdigest()


def chunk_markdown(body: str, version: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path, text in _sections(body):
        pieces = _pack(text)
        for i, piece in enumerate(pieces):
            if i > 0:
                piece = pieces[i - 1][-OVERLAP:].lstrip() + "\n\n" + piece
            chunks.append(Chunk(len(chunks), path, piece, _sha(version, path, piece)))
    return chunks
