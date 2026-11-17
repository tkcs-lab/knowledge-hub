"""propose_edit: starts from main, keeps governance keys, refusals, retry, shared limit (plan 16)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_hub.agent import texts
from knowledge_hub.auth.claims import Call
from knowledge_hub.config import Config
from knowledge_hub.indexer.frontmatter import parse_document
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools.agent_edit import propose_edit
from tests.integration.test_agent_propose_doc import REPOS, owners_yml
from tests.integration.test_derived_visibility_sql import insert
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all

REPO = "canonical-all"
PATH = "product/widget/specs/widget-spec.md"
DUE = (datetime.now(UTC) + timedelta(days=60)).date().isoformat()
OLD_DUE = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
# A hand-formatted file: quoted values, a trailing-space body, extra blank lines.
CURRENT = (
    "---\n"
    "id: widget-spec\n"
    "title: 'Widget specification'\n"
    "domain: product\n"
    f"review_by: {OLD_DUE}\n"
    "type: spec\n"
    "owner: owners-all-product\n"
    "supersedes: [old-spec]\n"
    "---\n"
    "\n"
    "# Widget  \n\nThe widget does things.\n\n\n"
)
BODY = "# Widget\n\nThe widget does things."


def args(target, **over):
    base = {
        "target_id": f"doc:{target}",
        "title": "Widget specification",
        "review_by": DUE,
        "body": "# Widget\n\nNow it does more.\n",
        "rationale": "The widget grew a feature.",
        "entities": [],
    }
    return {**base, **over}


@pytest.fixture
def app_settings(db_settings):
    from tests.support.mcp_client import HOST

    return db_settings.model_copy(
        update={
            "env": "test",
            "allowed_hosts": HOST,
            "agent_proposals_enabled": True,
            "agent_email_domains": "example.org",
        }
    )


@pytest.fixture
def forgejo():
    def repo(name: str) -> FakeRepo:
        files = {"owners.yml": owners_yml(name.removeprefix("canonical-")), PATH: CURRENT}
        return FakeRepo(commits=[FakeCommit("c1", files=files)])

    return ForgejoFake({r: repo(r) for r in REPOS} | {"notes-all": FakeRepo(commits=[FakeCommit("n1")])})


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool, forgejo):
    indexer = ForgejoFake({r: FakeRepo(commits=[FakeCommit("c1")]) for r in REPOS}, read_only=True)
    return Deps(
        settings=app_settings,
        config=Config(),
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        forgejo=indexer,
        forgejo_bot=forgejo,
    )


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


def add_doc(admin, *, repo=REPO, path=PATH, tier="T0", aud=("all",)) -> str:
    return insert(
        admin,
        "documents",
        dict(
            id=str(uuid.uuid4()),
            repo=repo,
            path=path,
            doc_key=path.rsplit("/", 1)[-1][:-3],
            tier=tier,
            domain="product",
            title="t",
            body="b",
            status="current",
            commit_sha="c1",
            content_sha="s",
            required_audiences=list(aud),
        ),
    )


@pytest.fixture
def target(admin):
    return add_doc(admin)


async def call(deps, target_id, caller=alice_all, **over):
    a = args(target_id, **over)
    state = {"kh_caller": caller, "kh_call": Call("propose_edit", a)}
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(scope={"state": state})))
    return await tool_guard("propose_edit", deps)(propose_edit)(ctx, **a)


async def refused(deps, target_id, text, caller=alice_all, **over):
    with pytest.raises(ToolError) as exc:
        await call(deps, target_id, caller, **over)
    assert str(exc.value).endswith(text), str(exc.value)


def rows(admin):
    return admin.execute("SELECT kind, state, path, doc_key FROM kh.proposals ORDER BY created_at").fetchall()


async def test_new_document_starts_from_main_and_keeps_governance_keys(deps, forgejo, admin, target):
    res = await call(deps, target, entities=[])
    assert res["existing"] is False and res["branch"] == "bot/propose/" + res["proposal_id"]
    parsed = parse_document(forgejo.files[(REPO, res["branch"], PATH)], PATH, Config().domains)
    assert (parsed.id, parsed.domain, parsed.type, parsed.owner) == (
        "widget-spec",
        "product",
        "spec",
        "owners-all-product",
    )
    assert parsed.supersedes == ["old-spec"] and parsed.status == "current"
    assert parsed.body.strip() == "# Widget\n\nNow it does more."
    assert parsed.review_by.isoformat() == DUE
    (pull,) = forgejo.opened
    assert pull["base"]["ref"] == "main" and "Edit " + PATH in forgejo.messages[0][3]
    assert rows(admin) == [("edit", "pr_open", PATH, "widget-spec")]
    assert not forgejo.repos[REPO].commits[-1].sha == res["branch"] and set(forgejo.written_refs) == {
        res["branch"]
    }


async def test_identical_edit_of_a_hand_formatted_file_is_no_change(deps, forgejo, admin, target):
    await refused(deps, target, texts.NO_CHANGE, review_by=OLD_DUE, body=BODY)
    assert not forgejo.opened and rows(admin) == []


async def test_changed_review_by_alone_is_a_change(deps, target):
    res = await call(deps, target, body=BODY)
    assert res["existing"] is False


async def test_t1_glossary_adr_and_missing_layout_are_not_editable(deps, admin):
    t1 = add_doc(admin, repo="notes-all", path="agent/a-note.md", tier="T1")
    await refused(deps, t1, texts.NOT_EDITABLE)
    t1_layout = add_doc(admin, repo="notes-all", path="product/widget/specs/note-spec.md", tier="T1")
    await refused(deps, t1_layout, texts.NOT_EDITABLE)
    glossary = add_doc(admin, path="glossary.md")
    await refused(deps, glossary, texts.NOT_EDITABLE)
    adr = add_doc(admin, path="product/widget/backend/adr/0001-choice.md")
    await refused(deps, adr, texts.NOT_EDITABLE)
    outside = add_doc(admin, path="product/old.md")
    await refused(deps, outside, texts.NOT_EDITABLE)
    assert rows(admin) == []


async def test_file_gone_from_main_is_not_editable(deps, forgejo, target):
    del forgejo.repos[REPO].commits[-1].files[PATH]
    await refused(deps, target, texts.NOT_EDITABLE)
    forgejo.repos[REPO].commits[-1].files[PATH] = "no frontmatter at all"
    await refused(deps, target, texts.NOT_EDITABLE)
    assert not forgejo.opened


async def test_other_audience_and_missing_id_give_the_same_not_found(deps, admin, forgejo):
    hidden = add_doc(admin, repo="canonical-mgmt", aud=("mgmt",))
    with pytest.raises(ToolError) as a:
        await call(deps, hidden)
    with pytest.raises(ToolError) as b:
        await call(deps, uuid.uuid4())
    assert str(a.value) == str(b.value) and str(a.value).endswith(errors.NOT_FOUND)
    assert not forgejo.opened


async def test_malformed_id_is_bad_id(deps):
    with pytest.raises(ToolError) as exc:
        await call(deps, "x")  # becomes doc:x
    assert errors.BAD_ID.split("{")[0] in str(exc.value)


async def test_identical_retry_returns_the_open_pull_request(deps, forgejo, admin, target):
    first = await call(deps, target)
    again = await call(deps, target)
    assert again["existing"] is True and again["pr_number"] == first["pr_number"]
    assert again["proposal_id"] == first["proposal_id"]
    assert len(forgejo.opened) == 1 and len(rows(admin)) == 1


async def test_flag_off(deps, app_settings, target):
    deps.settings = app_settings.model_copy(update={"agent_proposals_enabled": False})
    await refused(deps, target, texts.DISABLED_PROPOSALS)


async def test_daily_limit_is_shared_with_propose_doc(deps, app_settings, admin, forgejo, target):
    from tests.integration.test_agent_propose_doc import call as doc_call

    deps.settings = app_settings.model_copy(
        update={"agent_proposal_daily_limit": 1, "promotion_daily_limit": 50}
    )
    await doc_call(deps, id="one-spec")
    with pytest.raises(ToolError) as exc:
        await call(deps, target)
    assert "daily agent proposal limit (1) reached" in str(exc.value)
    assert forgejo.calls.count("open_pull") == 1
