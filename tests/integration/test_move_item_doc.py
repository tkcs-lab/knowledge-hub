"""move_item for documents over FakeForgejo: two pull requests, retries, refusals and the log line."""

from __future__ import annotations

import logging
from datetime import date

import psycopg
import pytest

from knowledge_hub.config import GroupConfig
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.forgejo import ForgejoBot, ForgejoError
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.server import Deps
from knowledge_hub.propose_render import render_file
from tests.support.bearer import TestBearerResolver
from tests.support.forgejo_fake import FakeForgejo, blob_sha
from tests.support.mcp_client import HOST
from tests.support.pg import FRESH_DB
from tests.support.principals import KH_CONFIG, multi
from tests.support.seed import add_doc, add_record

pytestmark = pytest.mark.integration

TITLE = "Moving guide"
BODY = "# Heading\n\nSome text.\n"


@pytest.fixture
def kh_config():
    return KH_CONFIG.model_copy(
        update={
            "labels": {"ember": "Ember product", "other": "Another label"},
            "archived_labels": ["other"],
            "groups": {
                **KH_CONFIG.groups,
                "all": GroupConfig(repo="kb-all", members=["alice", "multi"], approvers=["alice"]),
                "mgmt": GroupConfig(repo="kb-mgmt", members=["bob", "multi"], approvers=["bob"]),
            },
        }
    )


@pytest.fixture
def app_settings(db_settings, tmp_path):
    key = tmp_path / "cursor.key"
    key.write_bytes(b"k" * 32)
    return db_settings.model_copy(update={"env": "test", "allowed_hosts": HOST, "cursor_key_file": key})


@pytest.fixture
def forgejo() -> FakeForgejo:
    fake = FakeForgejo()
    fake.add_repo("kb-all")
    fake.add_repo("kb-mgmt")
    return fake


@pytest.fixture
def deps(app_settings, kh_config, app_pool, indexer_pool, forgejo) -> Deps:
    return Deps(
        settings=app_settings,
        config=kh_config,
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        embedder=FakeEmbedder(),
        bearer=TestBearerResolver(kh_config),
        forgejo_bot=ForgejoBot("https://forgejo.test", "tok", "kh", transport=forgejo.transport),
    )


@pytest.fixture
def db(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        yield conn
        conn.commit()


def make_doc(
    db, forgejo, key="guide", path="guides/g.md", title=TITLE, body=BODY, labels=(), commit=True, **extra
):
    """A document row of ``kb-all`` whose file is committed to the fake repo; returns ``(id, text)``."""
    text = render_file(key, title, "2027-01-01", list(labels), body, [])
    if commit:
        forgejo.commit("kb-all", {path: text})
    doc = add_doc(
        db,
        repo="kb-all",
        path=path,
        doc_key=key,
        audience="all",
        title=title,
        body=body,
        labels=list(labels),
        review_by=date(2027, 1, 1),
        blob_sha=blob_sha(text),
        **extra,
    )
    db.commit()
    return doc, text


async def move(mcp_client, id, from_group="all", to_group="mgmt", caller=multi):
    return await mcp_client.call(
        caller, "move_item", {"id": id, "from_group": from_group, "to_group": to_group}
    )


def pulls(forgejo, repo):
    return forgejo.repos[repo].pulls


async def test_move_opens_add_and_remove_pulls(mcp_client, db, forgejo):
    doc, text = make_doc(db, forgejo)
    main_all, main_mgmt = forgejo.head("kb-all"), forgejo.head("kb-mgmt")
    res = await move(mcp_client, f"doc:{doc}")
    assert not res.is_error, res.text
    assert res.data["tier"] == "T0" and res.data["id"] == f"doc:{doc}"
    assert res.data["add_pr_url"].endswith("/kb-mgmt/pulls/1") and res.data["add_pr_number"] == 1
    assert res.data["remove_pr_url"].endswith("/kb-all/pulls/1") and res.data["remove_pr_number"] == 1
    assert res.data["note"] == ""
    (add,) = pulls(forgejo, "kb-mgmt")
    (rem,) = pulls(forgejo, "kb-all")
    assert add["head"]["ref"].startswith("bot/move/") and rem["head"]["ref"].startswith("bot/move/")
    assert forgejo.files("kb-mgmt", add["head"]["ref"])["guides/g.md"] == text
    assert add["title"] == f"Move in: {TITLE}"
    assert add["body"].endswith(f"Moved-from: doc:{doc}")
    assert "guides/g.md" not in forgejo.files("kb-all", rem["head"]["ref"])
    assert rem["title"] == f"Move out: {TITLE}"
    assert rem["body"].startswith("Merge only after")
    assert rem["body"].endswith(f"Moved-from: doc:{doc}")
    assert forgejo.head("kb-all") == main_all and forgejo.head("kb-mgmt") == main_mgmt


async def test_result_lists_dropped_supersedes_and_revived(mcp_client, db, forgejo):
    doc, text = make_doc(db, forgejo, supersedes=["old-a", "old-b"])
    add_doc(db, repo="kb-all", path="old.md", doc_key="old-a", audience="all", superseded_by=doc)
    add_doc(db, repo="kb-all", path="old2.md", doc_key="old-c", audience="all", superseded_by=doc)
    db.commit()
    res = await move(mcp_client, f"doc:{doc}")
    assert not res.is_error, res.text
    assert res.data["dropped_supersedes"] == ["old-a", "old-b"]
    assert res.data["revived"] == ["old-a", "old-c"]
    (add,) = pulls(forgejo, "kb-mgmt")
    assert "supersedes" not in forgejo.files("kb-mgmt", add["head"]["ref"])["guides/g.md"]


async def test_wrong_from_group(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    res = await move(mcp_client, f"doc:{doc}", from_group="mgmt", to_group="all")
    assert res.is_error and res.text.endswith(errors.MOVE_WRONG_GROUP)
    assert not pulls(forgejo, "kb-mgmt") and not pulls(forgejo, "kb-all")


async def test_target_path_exists_is_conflict(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    forgejo.commit("kb-mgmt", {"guides/g.md": "something else\n"})
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.MOVE_CONFLICT)
    assert not pulls(forgejo, "kb-mgmt") and not pulls(forgejo, "kb-all")


async def test_target_doc_key_in_use_is_conflict(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    add_doc(db, repo="kb-mgmt", path="other.md", doc_key="guide", audience="mgmt")
    db.commit()
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.MOVE_CONFLICT)
    assert not pulls(forgejo, "kb-mgmt")


async def test_stale_blob_is_refused(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    forgejo.commit("kb-all", {"guides/g.md": "edited meanwhile\n"})
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.BLOB_CHANGED)
    assert not pulls(forgejo, "kb-mgmt")


async def test_superseded_document_is_linked(mcp_client, db, forgejo):
    newer, _ = make_doc(db, forgejo, key="newer", path="n.md")
    doc, _ = make_doc(db, forgejo, superseded_by=newer)
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.MOVE_LINKED)
    assert not pulls(forgejo, "kb-mgmt")


async def test_document_named_by_a_record_is_linked(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    add_record(
        db, outdated=True, outdated_at="2026-01-01T00:00:00Z", outdated_by="alice", replaced_by=f"doc:{doc}"
    )
    db.commit()
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.MOVE_LINKED)
    assert not pulls(forgejo, "kb-mgmt")


async def test_html_in_body_is_move_text(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo, body="Line<br>break\n")
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.MOVE_TEXT)
    assert not pulls(forgejo, "kb-mgmt")


async def test_archived_label_is_bad_label(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo, labels=["other"])
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.BAD_LABEL)
    assert not pulls(forgejo, "kb-mgmt")


async def test_second_call_returns_the_open_pulls(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    first = await move(mcp_client, f"doc:{doc}")
    second = await move(mcp_client, f"doc:{doc}")
    assert not second.is_error, second.text
    assert second.data == first.data
    assert len(pulls(forgejo, "kb-mgmt")) == 1 and len(pulls(forgejo, "kb-all")) == 1


async def test_only_the_add_pull_open_opens_the_removal(mcp_client, db, forgejo, deps, monkeypatch):
    doc, _ = make_doc(db, forgejo)
    real = deps.forgejo_bot.delete_file

    async def boom(*a, **k):
        raise ForgejoError("down")

    monkeypatch.setattr(deps.forgejo_bot, "delete_file", boom)
    first = await move(mcp_client, f"doc:{doc}")
    assert first.data["remove_pr_url"] == ""
    monkeypatch.setattr(deps.forgejo_bot, "delete_file", real)
    res = await move(mcp_client, f"doc:{doc}")
    assert not res.is_error, res.text
    assert res.data["add_pr_url"] == first.data["add_pr_url"]
    assert res.data["remove_pr_url"].endswith("/kb-all/pulls/1") and res.data["note"] == ""
    assert len(pulls(forgejo, "kb-mgmt")) == 1
    (rem,) = pulls(forgejo, "kb-all")
    assert rem["body"].startswith(f"Merge only after {first.data['add_pr_url']} is merged")


async def test_only_the_removal_pull_open_is_conflict(mcp_client, db, forgejo):
    doc, _ = make_doc(db, forgejo)
    head = forgejo.head("kb-all")
    forgejo.repos["kb-all"].branches["bot/move/x"] = head
    forgejo.repos["kb-all"].pulls.append(
        {
            "number": 1,
            "state": "open",
            "title": "t",
            "body": f"Merge only after x.\n\nMoved-from: doc:{doc}",
            "html_url": "https://forgejo.test/kh/kb-all/pulls/1",
            "head": {"ref": "bot/move/x", "sha": head},
            "base": {"ref": "main"},
            "user": {"login": forgejo.login},
        }
    )
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.MOVE_CONFLICT)
    assert not pulls(forgejo, "kb-mgmt")


async def test_removal_failure_returns_partial(mcp_client, db, forgejo, deps, monkeypatch):
    doc, _ = make_doc(db, forgejo)

    async def boom(*a, **k):
        raise ForgejoError("down")

    monkeypatch.setattr(deps.forgejo_bot, "delete_file", boom)
    res = await move(mcp_client, f"doc:{doc}")
    assert not res.is_error, res.text
    assert res.data["remove_pr_url"] == "" and res.data["remove_pr_number"] == 0
    assert res.data["add_pr_url"] != ""
    assert res.data["note"] == errors.MOVE_PARTIAL
    assert len(pulls(forgejo, "kb-mgmt")) == 1 and not pulls(forgejo, "kb-all")


async def test_add_failure_is_propose_failed(mcp_client, db, forgejo, deps, monkeypatch):
    doc, _ = make_doc(db, forgejo)

    async def boom(*a, **k):
        raise ForgejoError("down")

    monkeypatch.setattr(deps.forgejo_bot, "put_file", boom)
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.PROPOSE_FAILED)
    assert not pulls(forgejo, "kb-mgmt") and not pulls(forgejo, "kb-all")


async def test_retry_after_the_add_is_merged_opens_only_the_removal(mcp_client, db, forgejo):
    doc, text = make_doc(db, forgejo)
    forgejo.commit("kb-mgmt", {"guides/g.md": text})
    res = await move(mcp_client, f"doc:{doc}")
    assert not res.is_error, res.text
    assert res.data["add_pr_url"] == "" and res.data["add_pr_number"] == 0
    assert res.data["remove_pr_url"].endswith("/kb-all/pulls/1")
    assert not pulls(forgejo, "kb-mgmt")
    (rem,) = pulls(forgejo, "kb-all")
    assert rem["body"].startswith("The add pull request is already merged.")


async def test_open_pull_cap(mcp_client, db, forgejo, deps):
    doc, _ = make_doc(db, forgejo)
    deps.settings = deps.settings.model_copy(update={"propose_max_open": 1})
    forgejo.repos["kb-mgmt"].branches["bot/propose/x"] = forgejo.head("kb-mgmt")
    forgejo.repos["kb-mgmt"].pulls.append(
        {
            "number": 1,
            "state": "open",
            "title": "t",
            "body": "",
            "html_url": "u",
            "head": {"ref": "bot/propose/x", "sha": "s"},
            "base": {"ref": "main"},
            "user": {"login": forgejo.login},
        }
    )
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.TOO_MANY_OPEN)
    assert not pulls(forgejo, "kb-all")


async def test_not_configured_without_bot(mcp_client, db, forgejo, deps):
    doc, _ = make_doc(db, forgejo)
    deps.forgejo_bot = None
    res = await move(mcp_client, f"doc:{doc}")
    assert res.is_error and res.text.endswith(errors.NOT_CONFIGURED)


async def test_log_line(mcp_client, db, forgejo, caplog):
    doc, _ = make_doc(db, forgejo)
    with caplog.at_level(logging.INFO, logger="knowledge_hub.tools"):
        res = await move(mcp_client, f"doc:{doc}")
    assert not res.is_error, res.text
    assert f"move_item sub=multi id=doc:{doc} from=all to=mgmt" in caplog.text


async def test_retry_returning_open_pulls_logs_nothing(mcp_client, db, forgejo, caplog):
    doc, _ = make_doc(db, forgejo)
    assert not (await move(mcp_client, f"doc:{doc}")).is_error
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="knowledge_hub.tools"):
        res = await move(mcp_client, f"doc:{doc}")
    assert not res.is_error, res.text
    assert "move_item sub=" not in caplog.text


async def test_add_merged_with_failing_removal_logs_nothing(
    mcp_client, db, forgejo, deps, monkeypatch, caplog
):
    doc, text = make_doc(db, forgejo)
    forgejo.commit("kb-mgmt", {"guides/g.md": text})

    async def boom(*a, **k):
        raise ForgejoError("down")

    monkeypatch.setattr(deps.forgejo_bot, "delete_file", boom)
    with caplog.at_level(logging.INFO, logger="knowledge_hub.tools"):
        res = await move(mcp_client, f"doc:{doc}")
    assert not res.is_error, res.text
    assert res.data["note"] == errors.MOVE_PARTIAL
    assert "move_item sub=" not in caplog.text


async def test_unknown_document_is_not_found(mcp_client, db, forgejo):
    res = await move(mcp_client, "doc:00000000-0000-4000-8000-000000000000")
    assert res.is_error and res.text.endswith(errors.NOT_FOUND)
