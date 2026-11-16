import pytest

from knowledge_hub.governance import TYPE_FOLDERS
from knowledge_hub.governance.owners import (
    OwnersError,
    build_owners,
    dump_owners,
    glob_to_regex,
    load_owners,
    render_codeowners,
)

pytestmark = pytest.mark.unit

TEAMS = {
    "portfolio_team": "owners-portfolio",
    "product_team": "owners-product",
    "technical_team": "owners-technical",
    "owner_team": "owners-hr",
    "admin_team": "kh-admins",
}


def test_type_folders():
    assert TYPE_FOLDERS["adr"] == "adr"


@pytest.mark.parametrize("text", ["a: [", "- 1", "version: 2\naudience: all\ndomains: {}\nrules: []"])
def test_load_owners_errors(text):
    with pytest.raises(OwnersError, match="^owners.yml:"):
        load_owners(text)


def test_glob_to_regex():
    g = glob_to_regex("portfolio/**")
    assert g.fullmatch("portfolio/a/b.md") and not g.fullmatch("product/a.md")
    assert glob_to_regex("owners.yml").fullmatch("owners.yml")
    assert not glob_to_regex("owners.yml").fullmatch("ownersXyml")
    assert glob_to_regex("*.md").fullmatch("x.md")
    assert not glob_to_regex("*.md").fullmatch("a/b.md")
    assert glob_to_regex("a?c").fullmatch("abc") and not glob_to_regex("a?c").fullmatch("a/c")


def test_first_match_wins():
    o = build_owners("all", **TEAMS)
    assert o.rule_for("schema/x.yaml").team == "kh-admins"
    assert o.rule_for("portfolio/a.md").team == "owners-portfolio"
    assert o.rule_for("other/a.md").team == "kh-admins"


def test_build_shapes():
    a = build_owners("all", **TEAMS)
    assert [r.team for r in a.rules] == [
        "kh-admins",
        "owners-portfolio",
        "owners-product",
        "owners-technical",
        "kh-admins",
    ]
    assert a.rules[-1].paths == ["**"]
    hr = build_owners("hr", **TEAMS)
    assert hr.domains == {d: "owners-hr" for d in ("portfolio", "product", "technical")}
    assert [(r.team, r.paths[-1]) for r in hr.rules] == [("kh-admins", "glossary.md"), ("owners-hr", "**")]


@pytest.mark.parametrize("aud", ["all", "hr"])
def test_dump_round_trip(aud):
    o = build_owners(aud, **TEAMS)
    text = dump_owners(o)
    assert text.startswith("version: 1\naudience: " + aud + "\ndomains:")
    assert text.endswith("\n")
    assert load_owners(text) == o


def test_render_codeowners_all():
    o = build_owners("all", **TEAMS)
    assert render_codeowners(o, "acme") == (
        "# Generated from owners.yml; the CI check is authoritative.\n"
        "* @acme/kh-admins\n"
        "/technical/ @acme/owners-technical\n"
        "/product/ @acme/owners-product\n"
        "/portfolio/ @acme/owners-portfolio\n"
        "/.forgejo/ @acme/kh-admins\n"
        "/schema/ @acme/kh-admins\n"
        "/owners.yml @acme/kh-admins\n"
        "/CODEOWNERS @acme/kh-admins\n"
        "/glossary.md @acme/kh-admins\n"
    )
