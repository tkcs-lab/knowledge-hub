"""propose_doc: path, owner, requester record, idempotency, adoption, limits, citations (plan 16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_hub.agent import texts
from knowledge_hub.auth.claims import Call
from knowledge_hub.config import Config
from knowledge_hub.governance.owners import build_owners, dump_owners
from knowledge_hub.indexer.frontmatter import parse_document
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools import promote
from knowledge_hub.tools.agent_proposals import propose_doc
from tests.integration.test_derived_visibility_sql import doc as make_doc
from tests.integration.test_derived_visibility_sql import insert, uid
from tests.integration.test_propose_change import args as change_args
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, carol_hr

REPO = "canonical-all"
REPOS = ("canonical-all", "canonical-mgmt", "canonical-hr", "canonical-contracts")
DUE = (datetime.now(UTC) + timedelta(days=60)).date().isoformat()
BASE = {
    "audience": "all",
    "domain": "product",
    "product": "widget",
    "kind": "specs",
    "id": "widget-spec",
    "title": "Widget specification",
    "review_by": DUE,
    "body": "# Widget\n\nThe widget does things.\n",
    "rationale": "People keep asking how the widget works.",
    "entities": [],
    "component": "",
}
PATH = "product/widget/specs/widget-spec.md"


def owners_yml(audience: str) -> str:
    team = f"owners-{audience}"
    return dump_owners(
        build_owners(
            audience,
            portfolio_team=f"{team}-portfolio",
            product_team=f"{team}-product",
            technical_team=f"{team}-technical",
            owner_team=team,
            admin_team="admins",
        )
    )


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
        audience = name.removeprefix("canonical-")
        files = {"product/old.md": "old", "owners.yml": owners_yml(audience)}
        return FakeRepo(commits=[FakeCommit("c1", files=files)])

    return ForgejoFake({r: repo(r) for r in REPOS})


@pytest.fixture
def indexer_forgejo():
    return ForgejoFake({r: FakeRepo(commits=[FakeCommit("c1")]) for r in REPOS}, read_only=True)


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool, forgejo, indexer_forgejo):
    return Deps(
        settings=app_settings,
        config=Config(),
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        forgejo=indexer_forgejo,
        forgejo_bot=forgejo,
    )


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


async def call(deps, caller=alice_all, **over):
    args = {**BASE, **over}
    state = {"kh_caller": caller, "kh_call": Call("propose_doc", args)}
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(scope={"state": state})))
    return await tool_guard("propose_doc", deps)(propose_doc)(ctx, **args)


async def refused(deps, text, caller=alice_all, **over):
    with pytest.raises(ToolError) as exc:
        await call(deps, caller, **over)
    assert str(exc.value).endswith(text), str(exc.value)


def rows(admin):
    return admin.execute(
        "SELECT kind, state, branch, path, pr_number FROM kh.proposals ORDER BY created_at"
    ).fetchall()


# --- the proposal -------------------------------------------------------------------------------


async def test_opens_a_pull_request_with_path_type_owner_and_labels(deps, forgejo, admin):
    res = await call(deps)
    assert res["existing"] is False and res["pr_number"] == 1
    assert res["branch"] == "bot/propose/" + res["proposal_id"]
    (pull,) = forgejo.opened
    assert pull["base"]["ref"] == "main" and pull["head"]["ref"] == res["branch"]
    assert [lbl["name"] for lbl in pull["labels"]] == ["promotion", "domain/product"]
    text = forgejo.files[(REPO, res["branch"], PATH)]
    parsed = parse_document(text, PATH, Config().domains)
    assert (parsed.type, parsed.owner, parsed.id, parsed.domain) == (
        "spec",
        "owners-all-product",
        "widget-spec",
        "product",
    )
    assert parsed.body.strip() == "# Widget\n\nThe widget does things."
    assert "Title: Widget specification" in pull["body"]
    assert "Requested-by: alice <alice@example.org> (sub: alice)" in pull["body"]
    assert f"Proposal: {res['proposal_id']}" in pull["body"]
    (msg,) = forgejo.messages
    assert msg[:3] == (REPO, res["branch"], PATH) and msg[3].startswith(f"Propose {PATH}\n")
    assert [(k, s, b, p, n) for k, s, b, p, n in rows(admin)] == [("doc", "pr_open", res["branch"], PATH, 1)]


async def test_component_is_part_of_the_path(deps, forgejo):
    res = await call(deps, component="backend", id="api-spec")
    assert (REPO, res["branch"], "product/widget/backend/specs/api-spec.md") in forgejo.files


async def test_requester_record_is_posted_on_the_branch_head_between_write_and_pull(deps, forgejo):
    res = await call(deps)
    calls = forgejo.calls
    assert calls.index("put_file") < calls.index("post_requester") < calls.index("open_pull")
    head = forgejo.branches[(REPO, res["branch"])]
    (status,) = forgejo.statuses[(REPO, head)]
    assert status["description"] == f"alice {res['proposal_id']}" and status["context"] == "kh/requested-by"
    assert [k for k in forgejo.statuses] == [(REPO, head)]
    assert forgejo.opened[0]["head"]["sha"] == head


async def test_nothing_reaches_main(deps, forgejo):
    res = await call(deps)
    assert forgejo.written_refs and set(forgejo.written_refs) == {res["branch"]}
    assert (REPO, "main") not in forgejo.branches
    assert forgejo.repos[REPO].commits[-1].sha == "c1"
    assert not deps.forgejo.refused_writes and "put_note" not in forgejo.calls


async def test_identical_retry_returns_the_open_pull_request(deps, forgejo, admin):
    first = await call(deps)
    again = await call(deps)
    assert again == {
        "proposal_id": first["proposal_id"],
        "branch": first["branch"],
        "pr_number": first["pr_number"],
        "pr_url": first["pr_url"],
        "existing": True,
    }
    assert len(forgejo.opened) == 1 and len(rows(admin)) == 1


async def test_different_content_is_a_new_proposal(deps, forgejo):
    first = await call(deps)
    second = await call(deps, body="# Widget\n\nChanged.\n")
    assert second["existing"] is False and second["branch"] != first["branch"]


async def test_failed_attempt_is_retried_on_a_new_branch(deps, forgejo, admin):
    forgejo.labels[REPO] = set()  # open_pull fails: label not found
    with pytest.raises(ToolError) as exc:
        await call(deps)
    assert str(exc.value).endswith(texts.PROPOSAL_FAILED.format(tool="propose_doc"))
    assert [r[1] for r in rows(admin)] == ["failed"]
    forgejo.labels[REPO] = {"promotion", "domain/product"}
    res = await call(deps)
    assert res["existing"] is False and [r[1] for r in rows(admin)] == ["failed", "pr_open"]
    assert rows(admin)[0][2] != rows(admin)[1][2]  # a branch is never reused


async def test_foreign_commit_between_branch_and_write_fails_without_a_record(deps, forgejo, admin):
    def foreign() -> None:
        (key,) = [k for k in forgejo.branches if k[1].startswith("bot/propose/")]
        commit = FakeCommit("person1", parents=[forgejo.branches[key]], files={"notes.md": "mine"})
        forgejo.branch_commits.setdefault(REPO, []).append(commit)
        forgejo.branches[key] = commit.sha

    forgejo.on_put_file = foreign
    await refused(deps, texts.PROPOSAL_FAILED.format(tool="propose_doc"))
    assert not forgejo.statuses and not forgejo.opened and "post_requester" not in forgejo.calls
    assert [r[1] for r in rows(admin)] == ["failed"]


# --- pending rows ---------------------------------------------------------------------------------


def make_pending(admin, pid, age_minutes=0):
    admin.execute(
        "UPDATE kh.proposals SET state = 'pending', pr_number = NULL, pr_url = NULL,"
        " created_at = now() - make_interval(mins => %s) WHERE id = %s",
        (age_minutes, pid),
    )


async def test_pending_row_whose_pull_request_exists_is_adopted(deps, forgejo, admin):
    first = await call(deps)
    make_pending(admin, first["proposal_id"])
    again = await call(deps)
    assert again["existing"] is True and again["pr_number"] == 1 and again["branch"] == first["branch"]
    assert len(forgejo.opened) == 1
    assert rows(admin)[0][1] == "pr_open" and rows(admin)[0][4] == 1


async def test_pending_row_with_a_foreign_pull_request_is_not_adopted(deps, forgejo, admin):
    first = await call(deps)
    make_pending(admin, first["proposal_id"], age_minutes=10)
    forgejo.repos[REPO].pulls[0]["user"] = {"login": "mallory"}
    res = await call(deps)
    assert res["existing"] is False and res["branch"] != first["branch"]
    assert [r[1] for r in rows(admin)] == ["failed", "pr_open"]


async def test_old_pending_row_without_a_pull_request_becomes_failed_and_a_new_branch_is_used(
    deps, forgejo, admin
):
    first = await call(deps)
    make_pending(admin, first["proposal_id"], age_minutes=10)
    forgejo.repos[REPO].pulls.clear()
    res = await call(deps)
    assert res["existing"] is False and res["branch"] != first["branch"]
    assert [r[1] for r in rows(admin)] == ["failed", "pr_open"]
    assert forgejo.calls.count("create_branch") == 2


async def test_young_pending_row_without_a_pull_request_gives_proposal_failed(deps, forgejo, admin):
    first = await call(deps)
    make_pending(admin, first["proposal_id"], age_minutes=1)
    forgejo.repos[REPO].pulls.clear()
    before = forgejo.calls.count("create_branch")
    await refused(deps, texts.PROPOSAL_FAILED.format(tool="propose_doc"))
    assert forgejo.calls.count("create_branch") == before and len(forgejo.branches) == 1
    assert [r[1] for r in rows(admin)] == ["pending"]


# --- refusals ---------------------------------------------------------------------------------------


async def test_disabled_flag(deps, app_settings):
    deps.settings = app_settings.model_copy(update={"agent_proposals_enabled": False})
    await refused(deps, texts.DISABLED_PROPOSALS)


async def test_no_forgejo(deps):
    deps.forgejo_bot = None
    await refused(deps, promote.NO_FORGEJO)


async def test_person_only_and_audience_not_held(deps, forgejo):
    await refused(deps, texts.PERSON_ONLY, caller=carol_hr)
    await refused(deps, texts.AUDIENCE_NOT_HELD.format(a="mgmt"), audience="mgmt")
    assert not forgejo.opened


async def test_unknown_forgejo_user(deps, indexer_forgejo):
    indexer_forgejo.unknown_users.add("alice")
    await refused(deps, texts.BAD_LOGIN)


async def test_input_refusals_and_nothing_written(deps, forgejo, admin):
    await refused(deps, "domain must be one of portfolio, product, technical", domain="finance")
    await refused(deps, texts.BAD_ID_FORMAT, id="Bad_Id")
    await refused(deps, texts.BAD_TITLE, title="a <b>bold</b> title")
    await refused(deps, texts.BAD_REVIEW_BY, review_by="2020-01-01")
    await refused(deps, texts.BAD_BODY.format(n=131072), body="---\nid: x\n---\ntext")
    await refused(deps, texts.BAD_RATIONALE, rationale="")
    await refused(deps, texts.BAD_KIND, kind="adrs")
    await refused(deps, texts.BAD_SEGMENT, product="Bad Product")
    assert not forgejo.calls.count("create_branch") and rows(admin) == []


async def test_existing_path_and_id_in_use(deps, forgejo, admin):
    forgejo.repos[REPO].commits[-1].files[PATH] = "existing"
    await refused(deps, texts.DOC_EXISTS)
    insert(
        admin,
        "documents",
        dict(
            id=uid(),
            repo=REPO,
            path="product/other.md",
            doc_key="taken-id",
            tier="T0",
            domain="product",
            title="t",
            body="b",
            status="current",
            commit_sha="c1",
            content_sha="s",
            required_audiences=["all"],
        ),
    )
    await refused(deps, texts.ID_IN_USE.format(id="taken-id", repo=REPO), id="taken-id")
    assert rows(admin) == []


async def test_owners_file_problems_are_internal_errors(deps, forgejo):
    del forgejo.repos[REPO].commits[-1].files["owners.yml"]
    with pytest.raises(ToolError) as exc:
        await call(deps)
    assert "owners" not in str(exc.value) and "internal" in str(exc.value).lower()
    forgejo.repos[REPO].commits[-1].files["owners.yml"] = owners_yml("all").replace("product:", "other:")
    with pytest.raises(ToolError):
        await call(deps)
    assert not forgejo.opened and not forgejo.written_refs


# --- citations ----------------------------------------------------------------------------------------


async def test_citing_a_narrower_record_is_refused(deps, admin, forgejo):
    narrow = make_doc(admin, ("mgmt",))
    with pytest.raises(ToolError) as exc:
        await call(deps, bob_mgmt, rationale=f"see doc:{narrow}")
    assert str(exc.value) == errors.WIDER.format(repo="canonical-all", a="all", u="mgmt")
    assert not forgejo.opened and rows(admin) == []


async def test_citing_an_email_restricted_record_is_refused(deps, admin, forgejo):
    restricted = make_doc(admin, ("all",), ["alice@example.org"])
    await refused(deps, errors.EMAIL_SOURCES, body=f"# Widget\n\nBased on doc:{restricted}\n")
    assert not forgejo.opened


async def test_citing_a_wider_record_is_allowed(deps, admin, forgejo):
    wide = make_doc(admin, ("all",))
    res = await call(deps, bob_mgmt, audience="all", body=f"# Widget\n\nBased on doc:{wide}\n")
    assert res["existing"] is False


# --- limits ---------------------------------------------------------------------------------------------


async def test_per_requester_daily_limit(deps, app_settings, forgejo):
    deps.settings = app_settings.model_copy(
        update={"agent_proposal_daily_limit": 2, "promotion_daily_limit": 50}
    )
    await call(deps, id="one-spec")
    await call(deps, id="two-spec", domain="technical")
    with pytest.raises(ToolError) as exc:
        await call(deps, id="three-spec")
    assert "daily agent proposal limit (2) reached; try again after " in str(exc.value)
    assert str(exc.value).endswith(" UTC")
    await call(deps, bob_mgmt, id="bob-spec")  # another requester has his own count
    # A retry of an existing proposal is answered before any limit is read.
    assert (await call(deps, id="one-spec"))["existing"] is True


async def test_domain_repo_limit_counts_candidates_and_agent_rows(
    deps, app_settings, admin, mcp_client, forgejo
):
    deps.settings = app_settings.model_copy(
        update={"promotion_daily_limit": 2, "agent_proposal_daily_limit": 50}
    )
    source = make_doc(admin, ("all",))
    res = await mcp_client.call(alice_all, "propose_change", change_args([source]))
    assert not res.is_error, res.text
    await call(deps, id="one-spec")  # candidates 1 + agent rows 0 < 2
    await refused_limit(deps, id="two-spec")  # candidates 1 + agent rows 1 = 2
    await call(deps, id="tech-spec", domain="technical")  # another domain has its own count


async def refused_limit(deps, **over):
    with pytest.raises(ToolError) as exc:
        await call(deps, **over)
    text = errors.LIMIT.format(d="product", repo=REPO, n=2, time="")
    assert str(exc.value).startswith(text.split(" try again")[0]), str(exc.value)


async def test_agent_rows_never_stop_propose_change(deps, app_settings, admin, mcp_client, forgejo):
    deps.settings = app_settings.model_copy(
        update={"promotion_daily_limit": 1, "agent_proposal_daily_limit": 50}
    )
    await call(deps, id="one-spec")
    for n in range(3):
        await refused_limit_n(deps, id=f"extra-{n}")
    source = make_doc(admin, ("all",))
    res = await mcp_client.call(alice_all, "propose_change", change_args([source]))
    assert not res.is_error, res.text
    assert admin.execute("SELECT count(*) FROM kh.candidates").fetchone()[0] == 1


async def refused_limit_n(deps, **over):
    with pytest.raises(ToolError) as exc:
        await call(deps, **over)
    assert str(exc.value).startswith("daily promotion limit for domain product in canonical-all reached (1)")
