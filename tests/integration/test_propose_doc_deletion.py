"""propose_doc_deletion over FakeForgejo: the pull request, the refusals and the revived list."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from knowledge_hub.auth.claims import Caller
from knowledge_hub.forgejo import ForgejoBot
from knowledge_hub.mcp import errors
from knowledge_hub.propose_render import render_file
from tests.support.forgejo_fake import FakeForgejo, blob_sha
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all, dave_none
from tests.support.seed import add_doc, add_record

pytestmark = pytest.mark.integration

BODY = "# Heading\n\nSome text.\n"


@pytest.fixture
def forgejo() -> FakeForgejo:
    fake = FakeForgejo()
    fake.add_repo("kb-all")
    fake.add_repo("kb-hr")
    return fake


@pytest.fixture
def deps(deps, forgejo):
    deps.forgejo_bot = ForgejoBot("https://forgejo.test", "tok", "kh", transport=forgejo.transport)
    return deps


@pytest.fixture
def db(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        yield conn
        conn.commit()


def make_doc(db, forgejo, key="guide", path="guides/g.md", title="Guide", commit=True, **extra):
    """A T0 document of ``kb-all`` whose file is on the fake ``main``; returns ``(id, text)``."""
    text = render_file(key, title, "2027-01-01", [], BODY, [])
    if commit:
        forgejo.commit("kb-all", {path: text})
    doc = add_doc(
        db, repo="kb-all", path=path, doc_key=key, audience="all", title=title, body=BODY,
        review_by=date(2027, 1, 1), blob_sha=blob_sha(text), **extra,
    )
    db.commit()
    return doc, text


async def call(mcp_client, args, caller=alice_all):
    return await mcp_client.call(caller, "propose_doc_deletion", args)


def pulls(forgejo):
    return forgejo.repos["kb-all"].pulls


async def test_success(mcp_client, db, forgejo):
    doc, text = make_doc(db, forgejo)
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert not res.is_error, res.text
    branch = res.data["branch"]
    assert branch.startswith("bot/delete/")
    assert res.data["revived"] == [] and res.data["pr_number"] == 1
    assert res.data["pr_url"].endswith("/kb-all/pulls/1")
    assert "guides/g.md" not in forgejo.files("kb-all", branch)
    assert forgejo.files("kb-all")["guides/g.md"] == text
    (pull,) = pulls(forgejo)
    assert pull["title"] == "Delete: Guide"
    assert pull["body"] == (
        f"Requested-by: alice\nPrincipal: claude_client\nWritten-by: kh-bot\nDeletes: doc:{doc}"
    )


async def test_summary_is_in_the_body(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    res = await call(mcp_client, {"id": f"doc:{doc}", "summary": "Obsolete."})
    assert not res.is_error, res.text
    assert pulls(forgejo)[0]["body"].startswith("Obsolete.\n\nRequested-by: alice\n")


async def test_revived_lists_superseded_documents(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    old = add_doc(db, repo="kb-all", doc_key="old", superseded_by=doc)
    db.commit()
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert not res.is_error, res.text
    assert res.data["revived"] == [f"doc:{old}"]
    assert pulls(forgejo)[0]["body"].endswith(f"Deletes: doc:{doc}\n\nRevives: `doc:{old}`")


async def test_unknown_id(mcp_client, db, forgejo):
    res = await call(mcp_client, {"id": "doc:00000000-0000-4000-8000-000000000000"})
    assert res.is_error and res.text.endswith(errors.NOT_FOUND)
    assert not forgejo.requests


@pytest.mark.parametrize("bad", ["nonsense", "doc:1234", "rec:00000000-0000-4000-8000-000000000000"])
async def test_bad_id(mcp_client, db, forgejo, bad):
    res = await call(mcp_client, {"id": bad})
    assert res.is_error and res.text.endswith(errors.BAD_ARG.format(name="id"))
    assert not forgejo.requests


async def test_t0a_is_refused(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo, tier="T0a")
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert res.is_error and res.text.endswith(errors.DELETE_NOT_T0)
    assert not forgejo.requests


async def test_caller_without_the_group_sees_nothing(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    res = await call(mcp_client, {"id": f"doc:{doc}"}, caller=dave_none)
    assert res.is_error and res.text.endswith(errors.NOT_FOUND)
    assert not forgejo.requests


async def test_inactive_group_is_refused(mcp_client, db, forgejo):
    db.execute("INSERT INTO kh.groups (name, repo) VALUES (%s, %s)", ("finance", "kb-fin"))
    doc = add_doc(db, repo="kb-fin", doc_key="fin", audience="finance")
    db.commit()
    wf = Caller(
        sub="key:wf-fin", principal_type="n8n_workflow", email=None,
        audiences=frozenset({"finance"}), token_id=None,
    )
    res = await call(mcp_client, {"id": f"doc:{doc}"}, caller=wf)
    assert res.is_error and res.text.endswith(errors.AUDIENCE_NOT_HELD)
    assert not forgejo.requests


async def test_superseded_document_is_linked(mcp_client, db, forgejo):
    successor, _ = make_doc(db, forgejo, key="new", path="new.md")
    doc, _ = make_doc(db, forgejo, superseded_by=successor)
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert res.is_error and res.text.endswith(errors.DELETE_LINKED)
    assert not forgejo.requests


async def test_record_replaced_by_the_document_is_linked(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    add_record(db, outdated=True, outdated_at=date(2026, 1, 1), outdated_by="x", replaced_by=f"doc:{doc}")
    db.commit()
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert res.is_error and res.text.endswith(errors.DELETE_LINKED)
    assert not forgejo.requests


async def test_summary_too_large(mcp_client, db, forgejo, deps):
    deps.settings = deps.settings.model_copy(update={"propose_max_bytes": 100})
    doc, _ = make_doc(db, forgejo)
    res = await call(mcp_client, {"id": f"doc:{doc}", "summary": "x" * 101})
    assert res.is_error and res.text.endswith(errors.TOO_LARGE)
    assert not forgejo.requests


async def test_bad_text_in_summary(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    res = await call(mcp_client, {"id": f"doc:{doc}", "summary": "<b>x</b>"})
    assert res.is_error and res.text.endswith(errors.BAD_TEXT)
    assert not forgejo.requests


async def test_long_title_is_cut_in_the_pull_title(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo, title="T" * 200)
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert not res.is_error, res.text
    assert len(pulls(forgejo)[0]["title"]) == 120


async def test_stale_blob(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    forgejo.commit("kb-all", {"guides/g.md": render_file("guide", "Guide", "2027-02-02", [], BODY, [])})
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert res.is_error and res.text.endswith(errors.BLOB_CHANGED)
    assert not pulls(forgejo)


async def test_file_already_gone(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo, commit=False)
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert res.is_error and res.text.endswith(errors.BLOB_CHANGED)
    assert not pulls(forgejo)


async def test_second_call_is_pending(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    assert not (await call(mcp_client, {"id": f"doc:{doc}"})).is_error
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert res.is_error and res.text.endswith(errors.DELETE_PENDING)
    assert len(pulls(forgejo)) == 1


async def test_cap_reached(mcp_client, db, forgejo, deps):
    deps.settings = deps.settings.model_copy(update={"propose_max_open": 1})
    first, _ = make_doc(db, forgejo, key="a", path="a.md")
    second, _ = make_doc(db, forgejo, key="b", path="b.md")
    assert not (await call(mcp_client, {"id": f"doc:{first}"})).is_error
    res = await call(mcp_client, {"id": f"doc:{second}"})
    assert res.is_error and res.text.endswith(errors.TOO_MANY_OPEN)
    assert len(pulls(forgejo)) == 1


async def test_not_configured(mcp_client, db, forgejo, deps):
    doc, _ = make_doc(db, forgejo)
    deps.forgejo_bot = None
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert res.is_error and res.text.endswith(errors.NOT_CONFIGURED)


async def test_forgejo_failure(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    forgejo.fail_next("pulls", 500)
    res = await call(mcp_client, {"id": f"doc:{doc}"})
    assert res.is_error and res.text.endswith(errors.PROPOSE_FAILED)
