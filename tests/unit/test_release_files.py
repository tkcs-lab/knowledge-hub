from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text()


@pytest.fixture(scope="module")
def wf(text: str) -> dict:
    return yaml.safe_load(text)


def _triggers(wf: dict) -> dict:
    # YAML 1.1 parses the bare key `on` as boolean True.
    return wf["on"] if "on" in wf else wf[True]


def test_dispatch_has_only_dry_run_input(wf):
    dispatch = _triggers(wf)["workflow_dispatch"]
    assert list(dispatch["inputs"]) == ["dry_run"]
    assert dispatch["inputs"]["dry_run"]["type"] == "boolean"
    assert dispatch["inputs"]["dry_run"]["default"] is True


def test_top_level_permissions(wf):
    assert wf["permissions"] == {"contents": "read"}


def test_jobs(wf):
    assert set(wf["jobs"]) == {"build", "publish"}
    assert wf["jobs"]["build"]["runs-on"] == "ubuntu-24.04-arm"
    assert wf["jobs"]["publish"]["environment"] == "release"
    assert wf["jobs"]["publish"]["needs"] == "build"


def test_no_skip_checks_or_latest(text):
    assert "skip_checks" not in text
    assert ":latest" not in text


def test_every_run_step_is_strict(wf):
    runs = [
        step["run"]
        for job in wf["jobs"].values()
        for step in job["steps"]
        if "run" in step
    ]
    assert runs
    for run in runs:
        assert run.lstrip().startswith("set -euo pipefail"), run


def test_release_token_only_in_publish(wf):
    assert "TLD_RELEASE_TOKEN" not in yaml.safe_dump(wf["jobs"]["build"])
    assert "TLD_RELEASE_TOKEN" in yaml.safe_dump(wf["jobs"]["publish"])
