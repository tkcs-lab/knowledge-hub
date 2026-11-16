"""Pure validation rules for canonical repositories (plan sections 3.3 and 3.6)."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from knowledge_hub.config import Config
from knowledge_hub.governance import TYPE_FOLDERS
from knowledge_hub.governance.messages import Code, Finding
from knowledge_hub.governance.owners import OwnersFile
from knowledge_hub.indexer.frontmatter import ALLOWED_KEYS, Rejected, parse_document, raw_keys

ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
ADR_NAME_RE = re.compile(r"(\d{4})-[a-z0-9]+(-[a-z0-9]+)*")


@dataclass(frozen=True)
class Layout:
    domain: str
    product: str | None
    component: str | None
    kind: str | None
    name: str
    is_adr: bool
    is_glossary: bool


@dataclass(frozen=True)
class FileInput:
    path: str
    text: str


def parse_layout(path: str, domains: list[str]) -> Layout | None:
    parts = path.split("/")
    if any(p.startswith(".") for p in parts) or not path.endswith(".md"):
        return None
    if path == "glossary.md":
        return Layout("portfolio", None, None, None, "glossary", False, True)
    if len(parts) not in (4, 5) or parts[0] not in domains:
        return None
    stem = parts[-1][: -len(".md")]
    if not stem:
        return None
    product = parts[1]
    component = parts[2] if len(parts) == 5 else None
    kind = parts[-2]
    if kind not in TYPE_FOLDERS or not ID_RE.fullmatch(product):
        return None
    if component is not None and (not ID_RE.fullmatch(component) or component in TYPE_FOLDERS):
        return None
    is_adr = kind == "adr"
    if is_adr:
        if component is None or not ADR_NAME_RE.fullmatch(stem):
            return None
    elif not ID_RE.fullmatch(stem):
        return None
    return Layout(parts[0], product, component, kind, stem, is_adr, False)


def entity_kind_allowed(config: Config, kind: str, repo_audience: str) -> bool:
    """True when documents of ``repo_audience`` may cite entities of ``kind``."""
    spec = config.entities.get(kind)
    if spec is None:
        return False
    return spec.reference_audiences is None or repo_audience in spec.reference_audiences


def id_from_path(path: str, domains: list[str]) -> str | None:
    layout = parse_layout(path, domains)
    if layout is None:
        return None
    if layout.is_glossary:
        return "glossary"
    if layout.is_adr:
        return f"adr-{layout.component}-{layout.name[:4]}"
    return layout.name


def expected_type(layout: Layout) -> str:
    if layout.is_glossary:
        return "glossary"
    assert layout.kind is not None
    return TYPE_FOLDERS[layout.kind]


def validate_files(
    files: Sequence[FileInput],
    *,
    config: Config,
    owners: OwnersFile,
    ids_in_repo: Mapping[str, Collection[str]],
    ids_elsewhere: Collection[str],
    entity_keys: Mapping[str, Collection[str]],
    repo_audience: str,
) -> list[Finding]:
    from knowledge_hub.governance.messages import sort_findings

    kinds = "|".join(map(re.escape, config.entities))
    entity_re = re.compile(rf"({kinds}):[a-z0-9.-]+") if kinds else None
    out: list[Finding] = []
    for f in files:
        found: list[Finding] = []
        _validate_one(
            f, found, config, owners, ids_in_repo, ids_elsewhere, entity_keys, repo_audience, entity_re
        )
        seen: set[tuple[Code, str | None]] = set()
        for item in found:
            if (item.code, item.detail) not in seen:
                seen.add((item.code, item.detail))
                out.append(item)
    return sort_findings(out)


def _validate_one(
    f: FileInput,
    out: list[Finding],
    config: Config,
    owners: OwnersFile,
    ids_in_repo: Mapping[str, Collection[str]],
    ids_elsewhere: Collection[str],
    entity_keys: Mapping[str, Collection[str]],
    repo_audience: str,
    entity_re: re.Pattern[str] | None,
) -> None:
    def add(code: Code, detail: str | None = None) -> None:
        out.append(Finding(f.path, code, detail))

    layout = parse_layout(f.path, config.domains)
    if layout is None:
        add(Code.LAYOUT)
        return
    doc = parse_document(f.text, f.path, config.domains)
    if isinstance(doc, Rejected):
        add(Code.PARSE, doc.reason)
        return
    for key in raw_keys(f.text) or []:
        if key not in ALLOWED_KEYS:
            add(Code.UNKNOWN_KEY, str(key))
    if doc.domain != layout.domain:
        add(Code.DOMAIN, layout.domain)
    expected_id = id_from_path(f.path, config.domains)
    if not ID_RE.fullmatch(doc.id):
        add(Code.ID_FORMAT)
    elif doc.id != expected_id:
        add(Code.ID_FILENAME, expected_id)
    want_type = expected_type(layout)
    if not doc.type:
        add(Code.TYPE_MISSING)
    elif doc.type != want_type:
        add(Code.TYPE_PATH, want_type)
    team = owners.domains.get(layout.domain)
    if not doc.owner:
        add(Code.OWNER_MISSING)
    elif team is not None and doc.owner != team:
        add(Code.OWNER, team)

    for ref in doc.entities:
        if entity_re is None or not entity_re.fullmatch(ref):
            add(Code.ENTITY_FORMAT, ref)
            continue
        kind = ref.split(":", 1)[0]
        if not entity_kind_allowed(config, kind, repo_audience):
            add(Code.ENTITY_NOT_ALLOWED, ref)
        elif ref not in entity_keys.get(kind, ()):
            add(Code.ENTITY_NOT_FOUND, ref)

    for kind, value in (("prd", layout.product), ("cmp", layout.component)):
        if value is None or kind not in config.entities:
            continue
        if f"{kind}:{value}" not in entity_keys.get(kind, ()):
            add(Code.FOLDER_UNKNOWN)

    for target in [*doc.supersedes, *doc.superseded_by]:
        if target not in ids_in_repo or target == doc.id:
            add(Code.SUPERSEDES)

    holders = ids_in_repo.get(doc.id, ())
    if any(p != f.path for p in holders) or doc.id in ids_elsewhere:
        add(Code.ID_IN_USE)
