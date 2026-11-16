import os
import tomllib
from pathlib import Path

import pytest

from knowledge_hub.cli import main
from knowledge_hub.governance.owners import load_owners
from knowledge_hub.governance.render import render_governance
from knowledge_hub.governance.schema import dump_schema

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden"
CASES = [("canonical-all", "all"), ("canonical-hr", "hr")]


def _files(base: Path) -> list[str]:
    return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())


@pytest.mark.parametrize(("repo", "audience"), CASES)
def test_golden(tmp_path, repo, audience):
    out = tmp_path / repo
    render_governance(out, repo=repo, audience=audience)
    golden = GOLDEN / repo
    if os.environ.get("KH_UPDATE_GOLDEN") == "1":
        for rel in _files(out):
            target = golden / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((out / rel).read_bytes())
        return
    assert _files(out) == _files(golden)
    for rel in _files(out):
        assert (out / rel).read_bytes() == (golden / rel).read_bytes(), rel


AUDIENCES = ["all", "mgmt", "hr", "contracts"]
WORKFLOW = Path(".forgejo/workflows/kh.yml")


@pytest.mark.parametrize("audience", AUDIENCES)
def test_runner_label_golden(tmp_path, audience):
    out = tmp_path / "out"
    render_governance(
        out, repo=f"canonical-{audience}", audience=audience, runner_label=f"kh-validate-{audience}:host"
    )
    golden = GOLDEN / "runner-label" / audience / "kh.yml"
    if os.environ.get("KH_UPDATE_GOLDEN") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_bytes((out / WORKFLOW).read_bytes())
        return
    assert (out / WORKFLOW).read_bytes() == golden.read_bytes()
    default = tmp_path / "default"
    render_governance(default, repo=f"canonical-{audience}", audience=audience)
    for rel in _files(out):
        if rel != str(WORKFLOW):
            assert (out / rel).read_bytes() == (default / rel).read_bytes(), rel


@pytest.mark.parametrize("label", ["", "Bad Label", "kh-validate:docker", "a;b", "kh-validate\n"])
def test_runner_label_rejected(tmp_path, label):
    with pytest.raises(ValueError, match="runner label must match"):
        render_governance(tmp_path, repo="r", audience="all", runner_label=label)


def test_review_trigger(tmp_path):
    render_governance(tmp_path / "on", repo="r", audience="all", review_trigger=True)
    render_governance(tmp_path / "off", repo="r", audience="all")
    on = (tmp_path / "on/.forgejo/workflows/kh.yml").read_text()
    off = (tmp_path / "off/.forgejo/workflows/kh.yml").read_text()
    assert "  pull_request_review:\n    types: [submitted, dismissed]\n" in on
    assert "pull_request_review" not in off
    assert "{{REVIEW_TRIGGER}}" not in on + off


def test_workflow_has_no_checkout_or_inline_expressions(tmp_path):
    render_governance(tmp_path, repo="r", audience="all")
    text = (tmp_path / ".forgejo/workflows/kh.yml").read_text()
    assert "actions/checkout" not in text
    for line in text.splitlines():
        if line.strip().startswith(("kh ", "printf")):
            assert "${{" not in line


@pytest.mark.parametrize(("repo", "audience"), CASES)
def test_golden_owners_and_schema(repo, audience):
    owners = load_owners((GOLDEN / repo / "owners.yml").read_text())
    assert owners.audience == audience
    assert (GOLDEN / repo / "schema/frontmatter.schema.json").read_text() == dump_schema()


def test_templates_are_package_data():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    globs = data["tool"]["setuptools"]["package-data"]["knowledge_hub"]
    base = ROOT / "src" / "knowledge_hub"
    for path in (base / "governance" / "templates").rglob("*"):
        if path.is_file():
            assert any(path.relative_to(base).match(g) for g in globs), path


def test_bad_audience(tmp_path):
    with pytest.raises(ValueError, match="audience must match"):
        render_governance(tmp_path, repo="r", audience="Bad Audience")


def test_cli(tmp_path, capsys):
    argv = ["governance", "render", "--repo", "canonical-hr", "--audience", "hr", "--out", str(tmp_path)]
    code = main(argv)
    assert code == 0
    assert "wrote owners.yml" in capsys.readouterr().out
    assert (tmp_path / ".forgejo/CODEOWNERS").is_file()
    assert main(["governance", "render", "--repo", "r", "--audience", "BAD", "--out", str(tmp_path)]) == 2
    assert "audience must match" in capsys.readouterr().err
    main_argv = [*argv[:-1], str(tmp_path / "rl"), "--runner-label", "kh-validate-hr:host"]
    assert main(main_argv) == 0
    assert "runs-on: kh-validate-hr\n" in (tmp_path / "rl" / WORKFLOW).read_text()
    assert main([*argv[:-1], str(tmp_path / "bad"), "--runner-label", "Bad Label"]) == 2
    assert "runner label must match" in capsys.readouterr().err
