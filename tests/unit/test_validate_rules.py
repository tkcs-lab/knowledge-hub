from pathlib import Path

import pytest
import yaml

from knowledge_hub.config import Config
from knowledge_hub.governance.messages import Code, Finding, render_line, sort_findings
from knowledge_hub.governance.owners import build_owners
from knowledge_hub.governance.rules import (
    FileInput,
    entity_kind_allowed,
    expected_type,
    id_from_path,
    parse_layout,
    validate_files,
)
from knowledge_hub.indexer.frontmatter import ParsedDoc, parse_document

pytestmark = pytest.mark.unit

DATA = Path(__file__).parent.parent / "data" / "validate"
RAW = yaml.safe_load((DATA / "cases.yaml").read_text())
WORLD = RAW["world"]
CASES = RAW["cases"]
CONFIG = Config()
OWNERS = build_owners(
    "all",
    portfolio_team="owners-portfolio",
    product_team="owners-product",
    technical_team="owners-technical",
    owner_team="owners-hr",
    admin_team="kh-admins",
)


def run(name: str) -> list[Finding]:
    case = CASES[name]
    return validate_files(
        [FileInput(case["path"], (DATA / f"{name}.md").read_text())],
        config=CONFIG,
        owners=OWNERS,
        ids_in_repo=WORLD["ids_in_repo"],
        ids_elsewhere=WORLD["ids_elsewhere"],
        entity_keys=WORLD["entity_keys"],
        repo_audience=case.get("repo_audience", WORLD["repo_audience"]),
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_case_codes(name):
    assert sorted(f.code.name for f in run(name)) == sorted(CASES[name]["expect"])


@pytest.mark.parametrize("name", [n for n, c in sorted(CASES.items()) if not c["expect"]])
def test_passing_cases_are_indexable(name):
    doc = parse_document((DATA / f"{name}.md").read_text(), CASES[name]["path"], CONFIG.domains)
    assert isinstance(doc, ParsedDoc)


def test_every_code_is_exercised():
    used = {c for case in CASES.values() for c in case["expect"]}
    assert {c.name for c in Code} - {"TOO_LARGE", "TOO_MANY"} <= used


def test_render_line_leaks_nothing():
    foreign = {p for paths in WORLD["ids_in_repo"].values() for p in paths}
    for name, case in CASES.items():
        for f in run(name):
            line = render_line(f)
            assert line.startswith(f"{case['path']}: ")
            for other in foreign - {case["path"]}:
                assert other not in line


@pytest.mark.parametrize(
    ("path", "domain", "product", "component", "kind", "name", "ident", "typ"),
    [
        (
            "product/alpha/requirements/pay-limits.md",
            "product",
            "alpha",
            None,
            "requirements",
            "pay-limits",
            "pay-limits",
            "requirement",
        ),
        ("technical/gw/api/specs/rate.md", "technical", "gw", "api", "specs", "rate", "rate", "spec"),
        (
            "technical/gw/api/adr/0001-use-grpc.md",
            "technical",
            "gw",
            "api",
            "adr",
            "0001-use-grpc",
            "adr-api-0001",
            "adr",
        ),
        ("glossary.md", "portfolio", None, None, None, "glossary", "glossary", "glossary"),
    ],
)
def test_layout_shapes(path, domain, product, component, kind, name, ident, typ):
    layout = parse_layout(path, CONFIG.domains)
    assert layout is not None
    assert (layout.domain, layout.product, layout.component, layout.kind, layout.name) == (
        domain,
        product,
        component,
        kind,
        name,
    )
    assert id_from_path(path, CONFIG.domains) == ident
    assert expected_type(layout) == typ


@pytest.mark.parametrize(
    "path",
    [
        "technical/gw/adr/0001-x.md",
        "technical/gw/api/adr/use-grpc.md",
        "technical/gw/adr/adr/0001-x.md",
        "technical/gw/adr/specs/x.md",
        ".forgejo/pull_request_template.md",
        "product/alpha/.hidden/x.md",
        "product/alpha/requirements/.md",
        "other/alpha/requirements/x.md",
        "product/alpha/requirements/x.txt",
        "README.md",
    ],
)
def test_layout_rejects(path):
    assert parse_layout(path, CONFIG.domains) is None
    assert id_from_path(path, CONFIG.domains) is None


def test_render_and_sort():
    a = Finding("b.md", Code.OWNER, "x")
    b = Finding("a.md", Code.PARSE, "frontmatter: oops")
    assert [render_line(f) for f in sort_findings([a, b])] == [
        "a.md: frontmatter: oops",
        "b.md: owner must be x",
    ]


@pytest.mark.parametrize("audience", ["all", "mgmt", "hr", "contracts"])
def test_entity_kind_allowed_unrestricted_kinds(audience):
    assert entity_kind_allowed(CONFIG, "prd", audience)
    assert entity_kind_allowed(CONFIG, "cmp", audience)


def test_entity_kind_allowed_unlisted_kind():
    assert not entity_kind_allowed(CONFIG, "zzz", "all")


@pytest.mark.parametrize(
    ("audience", "expected"), [("contracts", True), ("mgmt", True), ("all", False), ("hr", False)]
)
def test_entity_kind_allowed_cus_default(audience, expected):
    assert entity_kind_allowed(CONFIG, "cus", audience) is expected
