"""pr_status and list_my_proposals: own rows only, one refusal, live PR state, approvals (plan 16)."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_hub.agent import store
from knowledge_hub.auth.claims import Call
from knowledge_hub.config import Config, RepoConfig
from knowledge_hub.mcp.errors import tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools.agent_status import list_my_proposals, pr_status
from tests.integration.test_derived_visibility_sql import insert, uid
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake, approval, pr
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt

HEAD = "a" * 40
NOT_FOUND = "not found"


@pytest.fixture
def forgejo():
    return ForgejoFake(
        {
            "canonical-all": FakeRepo(commits=[FakeCommit("k1", files={"README.md": "hi"})]),
            "notes-all": FakeRepo(commits=[FakeCommit("n0", files={"README.md": "hi"})]),
        },
        read_only=True,
    )


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool, forgejo):
    config = Config(
        repos={
            "canonical-all": RepoConfig(tier="T0", audience="all"),
            "notes-all": RepoConfig(tier="T1", audience="all"),
        }
    )
    return Deps(
        settings=app_settings, config=config, app_pool=app_pool, indexer_pool=indexer_pool, forgejo=forgejo
    )


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


def _ctx(state):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(scope={"state": state})))


async def run(deps, who, fn, name, **args):
    ctx = _ctx({"kh_caller": who, "kh_call": Call(name, args)})
    return await tool_guard(name, deps)(fn)(ctx, **args)


async def status(deps, who, proposal_id):
    return await run(deps, who, pr_status, "pr_status", proposal_id=proposal_id)


async def listing(deps, who, **args):
    full = {"only_open": False, "limit": 20, **args}  # the SDK dispatches the declared defaults too
    return await run(deps, who, list_my_proposals, "list_my_proposals", **full)


async def refused(awaitable, text):
    with pytest.raises(ToolError) as exc:
        await awaitable
    assert str(exc.value) == text


async def seed(deps, who, kind="doc", repo="canonical-all", login="alice", path=None, **kw):
    pid = uuid.uuid4()
    await store.insert_proposal(
        deps, who, id=pid, kind=kind, repo=repo, path=path or f"product/{pid}.md", doc_key=uid(),
        domain="product", title=f"title {pid}", content_sha="s", requester_login=login,
        dedupe_key=None if kind == "note" else uid(), **kw,
    )  # fmt: skip
    return pid


async def with_pr(deps, forgejo, who, *, login="alice", number=7, merged=False, state="open", sha=HEAD):
    """A doc proposal in ``pr_open`` with a fake PR whose head ref is its branch."""
    pid = uuid.uuid4()
    branch = f"bot/propose/{pid}"
    await store.insert_proposal(
        deps, who, id=pid, kind="doc", repo="canonical-all", path=f"product/{pid}.md", doc_key=uid(),
        domain="product", title="t", content_sha="s", requester_login=login, dedupe_key=uid(),
        branch=branch, base_sha="b1",
    )  # fmt: skip
    url = f"https://forgejo.test/kh/canonical-all/pulls/{number}"
    await store.mark(deps, who, pid, "pr_open", branch=branch, pr=(number, url))
    pull = pr(number, "m" * 40 if merged else "", sha, author="kh-hub-bot")
    pull["merged"] = merged
    pull["state"] = state
    pull["head"] = {"ref": branch, "sha": sha}
    if not merged:
        pull.pop("merge_commit_sha")
    forgejo.repos["canonical-all"].pulls.append(pull)
    return pid, pull


def poster_status(login, pid, **over):
    base = {"context": "kh/requested-by", "state": "success", "description": f"{login} {pid}",
            "creator": "kh-hub-bot"}  # fmt: skip
    return {**base, **over}


def ci(context, state, creator="kh-ci"):
    return {"context": context, "state": state, "description": "", "creator": creator}


# --- pr_status ------------------------------------------------------------------------------------


async def test_note_returns_stored_state_and_indexed_follows_commit(deps, admin):
    pid = await seed(deps, alice_all, kind="note", repo="notes-all", path="agent/n.md")
    await store.mark(deps, alice_all, pid, "committed", commit_sha="c1")
    res = await status(deps, alice_all, str(pid))
    assert res["state"] == "committed" and res["kind"] == "note" and res["checks"] == {}
    assert res["pr_number"] is None and res["pr_url"] is None and res["indexed"] is False
    assert res["merged"] is False and res["approved_by"] == [] and res["merge_commit"] is None
    key = admin.execute("SELECT doc_key FROM kh.proposals WHERE id = %s", (str(pid),)).fetchone()[0]
    row = dict(id=uid(), repo="notes-all", path="agent/n.md", doc_key=key, tier="T1", domain="product",
               title="t", body="b", status="current", commit_sha="other", content_sha="s",
               required_audiences=["all"], allowed_emails=None)  # fmt: skip
    insert(admin, "documents", row)
    assert (await status(deps, alice_all, str(pid)))["indexed"] is False
    admin.execute("UPDATE kh.documents SET commit_sha = 'c1' WHERE doc_key = %s", (key,))
    assert (await status(deps, alice_all, str(pid)))["indexed"] is True


async def test_doc_without_pr_returns_stored_state(deps, forgejo):
    pid = await seed(deps, alice_all)
    res = await status(deps, alice_all, str(pid))
    assert res["state"] == "pending" and res["checks"] == {} and res["pr_number"] is None
    await store.mark(deps, alice_all, pid, "failed", error="boom")
    res = await status(deps, alice_all, str(pid))
    assert res["state"] == "failed" and res["checks"] == {} and res["pr_url"] is None
    assert "pull" not in forgejo.calls


async def test_missing_foreign_and_malformed_ids_give_identical_errors(deps, forgejo):
    mine = await seed(deps, alice_all)
    foreign = await seed(deps, bob_mgmt, login="bob")
    texts = []
    for pid in (str(uuid.uuid4()), str(foreign), "not-a-uuid", ""):
        with pytest.raises(ToolError) as exc:
            await status(deps, alice_all, pid)
        texts.append(str(exc.value))
    assert texts == [NOT_FOUND] * 4
    assert (await status(deps, alice_all, str(mine)))["proposal_id"] == str(mine)
    assert forgejo.calls == []


async def test_open_pr_reports_checks_and_approvals(deps, forgejo):
    pid, _ = await with_pr(deps, forgejo, alice_all)
    forgejo.statuses[("canonical-all", HEAD)] = [
        ci("kh/validate", "failure"),  # newest first
        ci("kh/validate", "success"),
        ci("kh/domain-owners", "success"),
        poster_status("alice", pid),
    ]
    forgejo.repos["canonical-all"].reviews[7] = [approval(HEAD, "carol")]
    res = await status(deps, alice_all, str(pid))
    assert res["state"] == "open" and res["merged"] is False and res["merge_commit"] is None
    assert res["checks"] == {"kh/requested-by": "success", "kh/validate": "failure",
                             "kh/domain-owners": "success"}  # fmt: skip
    assert res["approved_by"] == ["carol"] and res["requester_approval_ignored"] is False
    assert res["pr_number"] == 7 and res["pr_url"].endswith("/pulls/7") and res["indexed"] is False


async def test_statuses_from_a_non_poster_are_missing(deps, forgejo):
    pid, _ = await with_pr(deps, forgejo, alice_all)
    forgejo.statuses[("canonical-all", HEAD)] = [
        ci("kh/validate", "success", creator="mallory"),
        ci("kh/domain-owners", "success", creator="mallory"),
        poster_status("alice", pid, creator="mallory"),
    ]
    res = await status(deps, alice_all, str(pid))
    assert res["checks"] == {"kh/requested-by": "missing", "kh/validate": "missing",
                             "kh/domain-owners": "missing"}  # fmt: skip


async def test_requester_approval_is_flagged_and_not_counted(deps, forgejo):
    pid, _ = await with_pr(deps, forgejo, alice_all)
    forgejo.statuses[("canonical-all", HEAD)] = [poster_status("alice", pid)]
    forgejo.repos["canonical-all"].reviews[7] = [approval(HEAD, "alice"), approval(HEAD, "carol")]
    res = await status(deps, alice_all, str(pid))
    assert res["approved_by"] == ["carol"] and res["requester_approval_ignored"] is True


async def test_approvals_are_sorted_and_stale_ones_ignored(deps, forgejo):
    pid, _ = await with_pr(deps, forgejo, alice_all)
    forgejo.statuses[("canonical-all", HEAD)] = [poster_status("alice", pid)]
    forgejo.repos["canonical-all"].reviews[7] = [
        approval(HEAD, "zed"), approval(HEAD, "bob"), approval("f" * 40, "old"),
    ]  # fmt: skip
    assert (await status(deps, alice_all, str(pid)))["approved_by"] == ["bob", "zed"]


async def test_merged_pr_reports_merge_commit_and_indexed(deps, forgejo, admin):
    pid, _ = await with_pr(deps, forgejo, alice_all, merged=True, number=9, state="closed")
    res = await status(deps, alice_all, str(pid))
    assert res["state"] == "merged" and res["merged"] is True and res["merge_commit"] == "m" * 40
    assert res["indexed"] is False
    insert(admin, "documents", dict(id=uid(), repo="canonical-all", path="product/z.md", doc_key=uid(),
           tier="T0", domain="product", title="t", body="b", status="current", commit_sha="m" * 40,
           content_sha="s", merge_pr=9, required_audiences=["all"], allowed_emails=None))  # fmt: skip
    assert (await status(deps, alice_all, str(pid)))["indexed"] is True


async def test_closed_unmerged_pr_is_closed(deps, forgejo):
    pid, _ = await with_pr(deps, forgejo, alice_all, state="closed")
    res = await status(deps, alice_all, str(pid))
    assert res["state"] == "closed" and res["merged"] is False


async def test_pr_with_another_head_ref_is_not_found(deps, forgejo):
    pid, pull = await with_pr(deps, forgejo, alice_all)
    pull["head"] = {"ref": "bot/propose/" + str(uuid.uuid4()), "sha": HEAD}
    await refused(status(deps, alice_all, str(pid)), NOT_FOUND)


# --- list_my_proposals ----------------------------------------------------------------------------


async def test_list_is_own_rows_newest_first(deps, admin):
    a = await seed(deps, alice_all)
    b = await seed(deps, alice_all, kind="note", repo="notes-all", path="agent/n.md")
    await seed(deps, bob_mgmt, login="bob")
    now = datetime.now(UTC)
    admin.execute("UPDATE kh.proposals SET created_at = %s WHERE id = %s", (now - timedelta(hours=1), str(a)))
    res = await listing(deps, alice_all)
    assert [p["proposal_id"] for p in res["proposals"]] == [str(b), str(a)]
    first = res["proposals"][0]
    assert set(first) == {"proposal_id", "kind", "repo", "path", "title", "state", "pr_number", "pr_url",
                          "created_at"}  # fmt: skip
    assert first["kind"] == "note" and first["state"] == "pending"
    datetime.fromisoformat(first["created_at"])


async def test_only_open_filters_states(deps):
    pending = await seed(deps, alice_all)
    failed = await seed(deps, alice_all)
    await store.mark(deps, alice_all, failed, "failed", error="x")
    note = await seed(deps, alice_all, kind="note", repo="notes-all", path="agent/n.md")
    await store.mark(deps, alice_all, note, "committed", commit_sha="c1")
    everything = {p["proposal_id"] for p in (await listing(deps, alice_all))["proposals"]}
    assert everything == {str(pending), str(failed), str(note)}
    opened = {p["proposal_id"] for p in (await listing(deps, alice_all, only_open=True))["proposals"]}
    assert opened == {str(pending), str(note)}


async def test_limit_bounds(deps):
    for _ in range(3):
        await seed(deps, alice_all)
    assert len((await listing(deps, alice_all, limit=2))["proposals"]) == 2
    assert len((await listing(deps, alice_all, limit=1))["proposals"]) == 1
    assert len((await listing(deps, alice_all, limit=50))["proposals"]) == 3
    for bad in (0, 51, -1):
        await refused(listing(deps, alice_all, limit=bad), "limit must be 1 to 50")
