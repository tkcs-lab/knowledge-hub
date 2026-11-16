"""Pure input rules of the agent write tools (plan sections 3.2 to 3.4, 3.6, 3.10).

No I/O. Every refusal raises ``KhError(code, text)``; every regex is applied with ``fullmatch``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from knowledge_hub.agent import texts
from knowledge_hub.auth.claims import Caller
from knowledge_hub.config import Config
from knowledge_hub.governance.rules import ID_RE, Layout, entity_kind_allowed, id_from_path, parse_layout
from knowledge_hub.mcp.errors import KhError

MAX_ID_CHARS = 80
MAX_TITLE_CHARS = 200
MAX_RATIONALE_CHARS = 2000
MAX_ENTITIES = 20
MAX_REVIEW_DAYS = 730
DOC_KINDS = ("requirements", "specs", "guides")

LOGIN_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,38}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_TAG_START_RE = re.compile(r"<[A-Za-z!?/]")
_MARKUP_START_RE = re.compile(r"<[A-Za-z/!?]")
_REFDEF_LINE_RE = re.compile(r"\s*\[[^\]]+\]:\s*\S.*")
_CITED_RE = re.compile(
    r"\b(doc|raw|derived):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)

_BAD_CATEGORIES = frozenset({"Cf", "Co", "Cs", "Cn", "Zl", "Zp"})
_BAD_RANGES = (
    (0xFE00, 0xFE0F),
    (0xE0100, 0xE01EF),
    (0x180B, 0x180D),
    (0x180F, 0x180F),
    (0x17B4, 0x17B5),
    (0x2800, 0x2800),
)
_BAD_POINTS = frozenset({0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0})


@dataclass(frozen=True)
class Requester:
    sub: str
    login: str
    email: str


def requester_from_caller(caller: Caller, allowed_domains: list[str]) -> Requester:
    if caller.principal_type != "claude_client" or not caller.email:
        raise KhError("person_only", texts.PERSON_ONLY)
    local, sep, domain = caller.email.lower().rpartition("@")
    allowed = {d.lower() for d in allowed_domains}
    if not sep or domain not in allowed or not LOGIN_RE.fullmatch(local):
        raise KhError("bad_login", texts.BAD_LOGIN)
    return Requester(sub=caller.sub, login=local, email=caller.email)


def check_id(value: str) -> str:
    if len(value) > MAX_ID_CHARS or not ID_RE.fullmatch(value):
        raise KhError("bad_id_format", texts.BAD_ID_FORMAT)
    return value


def _bad_char(ch: str) -> bool:
    cp = ord(ch)
    if cp in _BAD_POINTS:
        return True
    if any(lo <= cp <= hi for lo, hi in _BAD_RANGES):
        return True
    cat = unicodedata.category(ch)
    if cat in _BAD_CATEGORIES:
        return True
    return cat == "Cc" and ch not in "\n\t"


def clean_text(value: str, *, multiline: bool) -> str:
    text = unicodedata.normalize("NFC", value)
    if "<!--" in text or any(_bad_char(ch) for ch in text):
        raise KhError("bad_text", texts.BAD_TEXT)
    if not multiline and "\n" in text:
        raise KhError("bad_text", texts.BAD_TEXT)
    return text


def check_title(value: str) -> str:
    title = clean_text(value, multiline=False).strip()
    if not 1 <= len(title) <= MAX_TITLE_CHARS or _TAG_START_RE.search(title):
        raise KhError("bad_title", texts.BAD_TITLE)
    return title


def check_review_by(value: str, today: date) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise KhError("bad_review_by", texts.BAD_REVIEW_BY) from None
    if not today < parsed <= today + timedelta(days=MAX_REVIEW_DAYS):
        raise KhError("bad_review_by", texts.BAD_REVIEW_BY)
    return parsed


def check_entities(values: list[str], config: Config, repo_audience: str) -> list[str]:
    if len(values) > MAX_ENTITIES:
        raise KhError("bad_entity", texts.BAD_ENTITY)
    out: list[str] = []
    for ref in values:
        kind, sep, key = ref.partition(":")
        if (
            not sep
            or kind not in config.entities
            or not re.fullmatch(r"[a-z0-9.-]+", key)
            or not entity_kind_allowed(config, kind, repo_audience)
        ):
            raise KhError("bad_entity", texts.BAD_ENTITY)
        if ref not in out:
            out.append(ref)
    return out


def check_markup(value: str) -> str:
    if _MARKUP_START_RE.search(value) or any(_REFDEF_LINE_RE.fullmatch(ln) for ln in value.split("\n")):
        raise KhError("bad_markup", texts.BAD_MARKUP)
    return value


def _normalise(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def check_body(value: str, max_bytes: int) -> str:
    # CRLF and lone CR become LF before the hygiene check, which refuses every other control character.
    text = clean_text(value.replace("\r\n", "\n").replace("\r", "\n"), multiline=True)
    body = _normalise(text)
    first = next((ln for ln in body.split("\n") if ln.strip()), "")
    if not body or len(body.encode("utf-8")) > max_bytes or first.strip() == "---":
        raise KhError("bad_body", texts.BAD_BODY.format(n=max_bytes))
    return body


def check_rationale(value: str) -> str:
    text = clean_text(value, multiline=True)
    if not 1 <= len(text) <= MAX_RATIONALE_CHARS:
        raise KhError("bad_rationale", texts.BAD_RATIONALE)
    return text


def check_commit(value: str) -> str:
    if value and not COMMIT_RE.fullmatch(value):
        raise KhError("bad_commit", texts.BAD_COMMIT)
    return value


def note_path(id: str) -> str:
    return f"agent/{id}.md"


def doc_path(
    config: Config, domain: str, product: str, component: str, kind: str, id: str
) -> tuple[str, Layout]:
    if kind not in DOC_KINDS:
        raise KhError("bad_kind", texts.BAD_KIND)
    if not ID_RE.fullmatch(product) or (component and not ID_RE.fullmatch(component)):
        raise KhError("bad_segment", texts.BAD_SEGMENT)
    parts = [domain, product, *([component] if component else []), kind, f"{id}.md"]
    path = "/".join(parts)
    layout = parse_layout(path, config.domains)
    if (
        layout is None
        or layout.domain != domain
        or layout.is_adr
        or layout.is_glossary
        or id_from_path(path, config.domains) != id
    ):
        raise KhError("bad_path", texts.BAD_PATH)
    return path, layout


def cited_ids(*values: str) -> list[str]:
    found = {f"{kind.lower()}:{uid.lower()}" for text in values for kind, uid in _CITED_RE.findall(text)}
    return sorted(found)


def day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def next_day_text(now: datetime) -> str:
    return (day_start(now) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
