from __future__ import annotations

import pytest

from knowledge_hub.agent import guards, texts
from knowledge_hub.auth.claims import Caller
from knowledge_hub.config import Config, RepoConfig
from knowledge_hub.forgejo.client import ForgejoError, ForgejoRefused
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import KhError
from knowledge_hub.metrics import METRICS
from knowledge_hub.settings import Settings

pytestmark = pytest.mark.unit

HR_CONFIG = Config(
    repos={
        **Config().repos,
        "notes-hr": RepoConfig(tier="T1", audience="hr"),
    }
)


def refused(code, fn, *args):
    with pytest.raises(KhError) as exc:
        fn(*args)
    assert exc.value.code == code
    return exc.value


def caller(*audiences):
    return Caller(
        sub="s",
        principal_type="claude_client",
        email="a@x.org",
        audiences=frozenset(audiences),
        token_id=None,
    )


def test_require_audience():
    guards.require_audience(caller("all"), "all")
    exc = refused("audience_not_held", guards.require_audience, caller("all"), "hr")
    assert exc.text == texts.AUDIENCE_NOT_HELD.format(a="hr")


def test_t1_repo_lookup():
    assert guards.t1_repo(Config(), "all") == "notes-all"
    assert guards.t1_repo(HR_CONFIG, "hr") == "notes-hr"
    exc = refused("no_notes_repo", guards.t1_repo, Config(), "hr")
    assert exc.text == texts.NO_NOTES_REPO.format(a="hr")


def test_t0_repo_lookup():
    assert guards.t0_repo(Config(), "mgmt") == "canonical-mgmt"
    exc = refused("no_repo", guards.t0_repo, Config(), "finance")
    assert exc.text == errors.NO_REPO.format(a="finance")
    refused("no_repo", guards.t0_repo, Config(), "notes")  # a T1-only audience has no T0 repo


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("", []),
        ("example.org", ["example.org"]),
        (" Example.ORG , ,b.com,", ["example.org", "b.com"]),
    ],
)
def test_allowed_domains(raw, want):
    assert guards.allowed_domains(Settings(agent_email_domains=raw)) == want


def test_failure_metric_text():
    METRICS.reset()
    guards.record_write_failure("propose_doc", ForgejoRefused("x"))
    guards.record_write_failure("propose_doc", ForgejoError("y"))
    guards.record_write_failure("weird", RuntimeError("z"))
    text = METRICS.render()
    METRICS.reset()
    assert 'kh_agent_write_failures_total{tool="propose_doc",reason="refused"} 1' in text
    assert 'kh_agent_write_failures_total{tool="propose_doc",reason="forgejo_error"} 1' in text
    assert 'kh_agent_write_failures_total{tool="weird",reason="forgejo_error"} 1' in text
