"""write_note: guards, create, update, retry, ownership, limits, what reaches Forgejo (plan 16)."""

import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_hub.auth.claims import Call, Caller
from knowledge_hub.config import Config, RepoConfig
from knowledge_hub.mcp.errors import tool_guard
from knowledge_hub.mcp.server import Deps
from knowledge_hub.tools.agent_notes import write_note
from tests.integration.test_derived_visibility_sql import insert, uid
from tests.support.forgejo_fake import FakeCommit, FakeRepo, ForgejoFake
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, bob_mgmt, dave_none, wf_token

NOTES = ("notes-all", "notes-hr")
HR_ALICE = Caller(
    sub="alice-hr", principal_type="claude_client", email="alice@example.org",
    audiences=frozenset({"all", "hr"}), token_id=None,
)  # fmt: skip
ALICE_OTHER_TOKEN = Caller(
    sub="alice-2", principal_type="claude_client", email="alice@example.org",
    audiences=frozenset({"all"}), token_id=None,
)  # fmt: skip
SHA = "0" * 40


def review_by(days: int = 30) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


def hexsha(n: int) -> str:
    return f"{n:040x}"


C1, C2 = hexsha(0xC1), hexsha(0xC2)


class HexFake(ForgejoFake):
    """The fake names its commits ``n1``; ``base_commit`` must be a hex sha, so rename them."""

    async def put_note(self, repo, path, content, message, prev_blob):
        try:
            await super().put_note(repo, path, content, message, prev_blob)
        finally:
            commit = self.repos[repo].commits[-1]
            if not commit.sha.startswith("0"):
                commit.sha = hexsha(0xA000 + self._counter)
        return commit.sha


def _repos(**files: str) -> dict[str, FakeRepo]:
    return {
        "notes-all": FakeRepo(commits=[FakeCommit(C1, files={"README.md": "hi", **files})]),
        "notes-hr": FakeRepo(commits=[FakeCommit("h1", files={"README.md": "hi"})]),
        "canonical-all": FakeRepo(commits=[FakeCommit("k1", files={"product/old.md": "old"})]),
    }


@pytest.fixture
def notes():
    return HexFake(_repos())


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool, notes):
    settings = app_settings.model_copy(
        update={"agent_notes_enabled": True, "agent_email_domains": "example.org"}
    )
    config = Config(
        repos={
            "canonical-all": RepoConfig(tier="T0", audience="all"),
            "notes-all": RepoConfig(tier="T1", audience="all"),
            "notes-hr": RepoConfig(tier="T1", audience="hr"),
        }
    )
    return Deps(
        settings=settings,
        config=config,
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        forgejo=ForgejoFake(_repos(), read_only=True),
        forgejo_notes=notes,
    )


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


def _fake_ctx(state):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(scope={"state": state})))


def note_args(**over):
    base = {
        "audience": "all",
        "id": "my-note",
        "title": "My note",
        "domain": "product",
        "review_by": review_by(),
        "body": "First line.\n\nSecond line.",
        "entities": [],
        "base_commit": "",
    }
    base.update(over)
    return base


async def call(deps, who, **over):
    args = note_args(**over)
    ctx = _fake_ctx({"kh_caller": who, "kh_call": Call("write_note", args)})
    return await tool_guard("write_note", deps)(write_note)(ctx, **args)


async def refused(deps, who, text, **over):
    with pytest.raises(ToolError) as exc:
        await call(deps, who, **over)
    assert str(exc.value) == text


def rows(admin):
    return admin.execute(
        "SELECT kind, state, repo, path, requester_login, created_by_sub, commit_sha FROM kh.proposals"
        " ORDER BY created_at"
    ).fetchall()


def assert_only_notes_main(notes):
    assert set(notes.written_refs) <= {"main"}
    assert all(repo in NOTES for repo, _, _, _ in notes.messages)
    assert notes.calls.count("create_branch") == notes.calls.count("open_pull") == 0
    assert notes.refused_writes == []


# --- create, update, retry ------------------------------------------------------------------------


async def test_create_writes_one_commit_with_trailers(deps, notes, admin):
    res = await call(deps, alice_all)
    assert res["created"] is True and res["unchanged"] is False
    assert (res["repo"], res["path"]) == ("notes-all", "agent/my-note.md")
    assert res["indexed"] == "after the next index run"
    (repo, branch, path, message) = notes.messages[0]
    assert (repo, branch, path) == ("notes-all", "main", "agent/my-note.md")
    assert "Agent note my-note" in message.splitlines()[0]
    assert "Written-by: agent via knowledge-hub (write_note)" in message
    assert "Requested-by: alice <alice@example.org> (sub: alice)" in message
    assert f"Proposal: {res['proposal_id']}" in message
    text = notes.repos["notes-all"].commits[-1].files["agent/my-note.md"]
    assert "type: agent-note" in text and "owner: alice" in text
    assert len(notes.messages) == 1
    assert_only_notes_main(notes)
    assert notes.repos["canonical-all"].commits[-1].sha == "k1"
    assert rows(admin) == [
        ("note", "committed", "notes-all", "agent/my-note.md", "alice", "alice", res["commit_sha"])
    ]


async def test_repeated_call_is_unchanged_and_writes_nothing(deps, notes, admin):
    first = await call(deps, alice_all)
    again = await call(deps, alice_all)
    assert again == {
        "proposal_id": "", "repo": "notes-all", "path": "agent/my-note.md",
        "commit_sha": first["commit_sha"], "created": False, "unchanged": True,
        "indexed": "after the next index run",
    }  # fmt: skip
    assert len(notes.messages) == 1 and len(rows(admin)) == 1


async def test_update_needs_base_commit_and_changes_the_file(deps, notes, admin):
    first = await call(deps, alice_all)
    await refused(
        deps, alice_all, "a note with this id exists; pass base_commit, the commit_sha you read",
        body="Changed.",
    )  # fmt: skip
    res = await call(deps, alice_all, body="Changed.", base_commit=first["commit_sha"])
    assert res["created"] is False and res["unchanged"] is False
    assert res["commit_sha"] != first["commit_sha"] and len(notes.messages) == 2
    assert "Changed." in notes.repos["notes-all"].commits[-1].files["agent/my-note.md"]
    assert [r[1] for r in rows(admin)] == ["committed", "committed"]
    assert_only_notes_main(notes)


async def test_stale_base_commit_names_the_current_one(deps):
    first = await call(deps, alice_all)
    await refused(
        deps, alice_all, f"note changed since you read it (now {first['commit_sha']})",
        body="Changed.", base_commit=SHA,
    )  # fmt: skip


async def test_base_commit_without_a_note_is_missing(deps, notes):
    await refused(
        deps, alice_all, "no note with this id exists; leave base_commit empty to create it",
        base_commit=SHA,
    )  # fmt: skip
    assert notes.messages == []


async def test_lost_response_after_commit_then_retry_settles_the_row(deps, notes, admin):
    notes.fail_after_commit = True
    await refused(deps, alice_all, FAILED)
    assert [r[1] for r in rows(admin)] == ["failed"]
    assert len(notes.messages) == 1  # the note is committed although the call failed
    notes.fail_after_commit = False
    res = await call(deps, alice_all)
    assert res["unchanged"] is True and res["proposal_id"] == ""
    assert len(notes.messages) == 1
    state = rows(admin)
    assert [r[1] for r in state] == ["committed"] and state[0][6] == res["commit_sha"]


async def test_failed_write_that_left_nothing_is_retried_with_a_new_row(deps, notes, admin):
    real = notes.put_note

    async def boom(*a, **kw):
        from knowledge_hub.forgejo.client import ForgejoError

        raise ForgejoError("PUT contents failed: HTTP 500")

    notes.put_note = boom
    await refused(deps, alice_all, FAILED)
    notes.put_note = real
    res = await call(deps, alice_all)
    assert res["created"] is True
    assert [r[1] for r in rows(admin)] == ["failed", "committed"]


# --- ownership -------------------------------------------------------------------------------------


BAD_TEXT = "text must not contain control, hidden or bidirectional characters or HTML comments"
BAD_MARKUP = "note text must not contain HTML tags or link reference definitions"
BAD_BODY = "body must be 1 to 131072 bytes and must not start with a frontmatter block"
FAILED = "could not write the note; call write_note again with the same arguments"
NOT_YOURS = "only your own agent notes can be updated"


async def test_other_login_cannot_update(deps):
    first = await call(deps, alice_all)
    await refused(deps, bob_mgmt, NOT_YOURS, body="Mine now.", base_commit=first["commit_sha"])


async def test_same_login_other_token_cannot_update(deps):
    first = await call(deps, alice_all)
    await refused(deps, ALICE_OTHER_TOKEN, NOT_YOURS, body="x", base_commit=first["commit_sha"])


async def test_human_file_at_the_path_is_not_yours(deps, notes):
    notes.repos["notes-all"].commits.append(
        FakeCommit(C2, parents=[C1], files={"agent/my-note.md": "# Human\n"})
    )
    await refused(deps, alice_all, NOT_YOURS, base_commit=C2)
    assert notes.messages == []


async def test_note_of_another_type_is_not_yours(deps, notes):
    text = "---\nid: my-note\ntitle: T\ndomain: product\nreview_by: 2030-01-01\n"
    text += "type: spec\nowner: alice\n---\nx\n"
    notes.repos["notes-all"].commits.append(FakeCommit(C2, parents=[C1], files={"agent/my-note.md": text}))
    await refused(deps, alice_all, NOT_YOURS, base_commit=C2)


# --- id in use ---------------------------------------------------------------------------------------


async def test_human_file_with_the_same_stem_blocks_the_id(deps, notes):
    notes.repos["notes-all"].commits.append(
        FakeCommit(C2, parents=[C1], files={"product/my-note.md": "# Human\n"})
    )
    await refused(deps, alice_all, "the id my-note is already used in notes-all")
    assert notes.messages == []


async def test_document_row_at_another_path_blocks_the_id(deps, admin):
    insert(
        admin,
        "documents",
        dict(
            id=uid(), repo="notes-all", path="product/elsewhere.md", doc_key="my-note", tier="T1",
            domain="product", title="t", body="b", status="current", commit_sha=C1, content_sha="s",
            required_audiences=["all"], allowed_emails=None,
        ),
    )  # fmt: skip
    await refused(deps, alice_all, "the id my-note is already used in notes-all")


# --- who may call ------------------------------------------------------------------------------------


async def test_flag_off_is_refused(deps):
    off = dataclasses.replace(deps, settings=deps.settings.model_copy(update={"agent_notes_enabled": False}))
    await refused(off, alice_all, "agent notes are not enabled on this server")


async def test_missing_notes_connection_is_refused(deps):
    gone = dataclasses.replace(deps, forgejo_notes=None)
    await refused(
        gone, alice_all, "agent notes are not available: no Forgejo notes connection is configured"
    )  # fmt: skip


async def test_n8n_and_callers_without_email_are_refused(deps):
    text = "agent writes are limited to people with a verified email address"
    await refused(deps, wf_token, text)
    no_email = dataclasses.replace(alice_all, email=None)
    await refused(deps, no_email, text)


async def test_email_domain_not_allowed_or_not_in_the_organisation(deps, notes):
    other = dataclasses.replace(alice_all, email="alice@elsewhere.test")
    text = "your email address cannot be mapped to a Forgejo login"
    await refused(deps, other, text)
    deps.forgejo.unknown_users.add("alice")
    await refused(deps, alice_all, text)
    assert notes.messages == []


async def test_audience_not_held(deps, notes):
    await refused(deps, alice_all, "your token does not hold the audience hr", audience="hr")
    await refused(deps, dave_none, "your token does not hold the audience all")
    assert notes.messages == []


async def test_audience_without_a_notes_repo(deps):
    await refused(deps, bob_mgmt, "no notes repository is configured for audience mgmt", audience="mgmt")


async def test_audience_picks_its_own_notes_repo(deps, notes):
    res = await call(deps, HR_ALICE, audience="hr")
    assert res["repo"] == "notes-hr"
    assert [m[0] for m in notes.messages] == ["notes-hr"]


# --- input rules ------------------------------------------------------------------------------------


async def test_unknown_domain(deps):
    await refused(deps, alice_all, "domain must be one of portfolio, product, technical", domain="nope")


@pytest.mark.parametrize(
    "over,text",
    [
        ({"id": "Bad_Id"}, "id must be lowercase letters, digits and single hyphens, at most 80 characters"),
        ({"title": "a <b>tag"}, "title must be one line of 1 to 200 characters, without markup"),
        ({"title": ""}, "title must be one line of 1 to 200 characters, without markup"),
        (
            {"review_by": "2001-01-01"},
            "review_by must be a date (YYYY-MM-DD) after today and within 730 days",
        ),
        (
            {"entities": ["nope:x"]},
            "entities must be at most 20 ids of the form kind:key for the kinds this repository may cite",
        ),
        (
            {"body": "a<!-- hidden -->"},
            "text must not contain control, hidden or bidirectional characters or HTML comments",
        ),
        (
            {"body": "<script>x</script>"},
            "note text must not contain HTML tags or link reference definitions",
        ),
        (
            {"body": "---\nid: x\n---\nbody"},
            "body must be 1 to 131072 bytes and must not start with a frontmatter block",
        ),
        ({"base_commit": "xyz"}, "base_commit must be a commit sha"),
    ],
)
async def test_input_refusals_write_nothing(deps, notes, admin, over, text):
    await refused(deps, alice_all, text, **over)
    assert notes.messages == [] and rows(admin) == []


async def test_body_over_the_size_limit(deps, notes):
    small = dataclasses.replace(deps, settings=deps.settings.model_copy(update={"agent_max_body_bytes": 10}))
    await refused(
        small, alice_all, "body must be 1 to 10 bytes and must not start with a frontmatter block",
        body="x" * 11,
    )  # fmt: skip
    assert notes.messages == []


async def test_citation_of_a_wider_record_is_refused(deps, admin, notes):
    from tests.integration.test_derived_visibility_sql import doc as make_doc

    mgmt_doc = make_doc(admin, ("mgmt",))
    with pytest.raises(ToolError):
        await call(deps, bob_mgmt, body=f"see doc:{mgmt_doc}")
    assert notes.messages == []


# --- limit ----------------------------------------------------------------------------------------


async def test_daily_limit_counts_notes_and_failed_attempts(deps, notes, admin):
    limited = dataclasses.replace(
        deps, settings=deps.settings.model_copy(update={"agent_note_daily_limit": 2})
    )
    await call(limited, alice_all, id="n-one")
    notes.fail_after_commit = True
    await refused(
        limited,
        alice_all,
        FAILED,
        id="n-two",
    )
    notes.fail_after_commit = False
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d 00:00")
    text = f"daily agent note limit (2) reached; try again after {tomorrow} UTC"
    await refused(limited, alice_all, text, id="n-three")
    assert len(notes.messages) == 2
    # An unchanged retry uses no limit and no row.
    assert (await call(limited, alice_all, id="n-one"))["unchanged"] is True
    assert len(rows(admin)) == 2


async def test_nothing_reaches_a_canonical_repo(deps, notes):
    await call(deps, alice_all)
    await call(deps, HR_ALICE, audience="hr", id="hr-note")
    assert {m[0] for m in notes.messages} == {"notes-all", "notes-hr"}
    assert notes.repos["canonical-all"].commits[-1].sha == "k1"
