"""propose_doc over FakeForgejo: create, edit, races, caps, refusals and the pull request text."""

from __future__ import annotations

import pytest

from knowledge_hub.forgejo import ForgejoBot, ForgejoRefused
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import (
    AUDIENCE_NOT_HELD,
    BAD_ARG,
    BAD_LABEL,
    BAD_TEXT,
    BLOB_CHANGED,
    ID_IN_USE,
    NOT_CONFIGURED,
    PATH_EXISTS,
    PROPOSE_FAILED,
    TOO_LARGE,
    TOO_MANY_OPEN,
)
from tests.support.forgejo_fake import FakeForgejo, blob_sha, md
from tests.support.principals import alice_all, dave_none, wf_token

pytestmark = pytest.mark.integration

ARGS = {
    "group": "all",
    "path": "guides/new.md",
    "id": "new-guide",
    "title": "A new guide",
    "review_by": "2027-06-30",
    "body": "# Heading\n\nSome text.\n",
}


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


async def call(mcp_client, args=None, caller=alice_all):
    return await mcp_client.call(caller, "propose_doc", {**ARGS, **(args or {})})


async def test_create(mcp_client, seeded_db, forgejo):
    res = await call(mcp_client, {"labels": ["ember"], "summary": "Why this."})
    assert not res.is_error, res.text
    branch = res.data["branch"]
    assert branch.startswith("bot/propose/")
    assert res.data["pr_number"] == 1 and res.data["pr_url"].endswith("/kb-all/pulls/1")
    assert "guides/new.md" not in forgejo.files("kb-all")  # main is untouched
    text = forgejo.files("kb-all", branch)["guides/new.md"]
    assert text == (
        '---\nid: "new-guide"\ntitle: "A new guide"\nreview_by: "2027-06-30"\nlabels: ["ember"]\n---\n'
        "# Heading\n\nSome text.\n"
    )
    (pull,) = forgejo.repos["kb-all"].pulls
    assert pull["title"] == "Propose: A new guide"
    assert pull["base"]["ref"] == "main" and pull["head"]["ref"] == branch
    assert pull["body"] == "Why this.\n\nRequested-by: alice\nPrincipal: claude_client\nWritten-by: kh-bot"
    parents = forgejo.repos["kb-all"].commits[forgejo.head("kb-all", branch)]
    assert parents.parents == [forgejo.head("kb-all")]


async def test_pull_body_without_summary_and_workflow_principal(mcp_client, seeded_db, forgejo):
    res = await call(mcp_client, caller=wf_token)
    assert not res.is_error, res.text
    (pull,) = forgejo.repos["kb-all"].pulls
    assert pull["body"] == "Requested-by: n8n-token:t1\nPrincipal: n8n_workflow\nWritten-by: kh-bot"


async def test_long_title_is_cut_in_the_pull_title(mcp_client, seeded_db, forgejo):
    res = await call(mcp_client, {"title": "T" * 300})
    assert not res.is_error, res.text
    assert len(forgejo.repos["kb-all"].pulls[0]["title"]) == 120


async def test_edit_with_base_blob(mcp_client, seeded_db, forgejo):
    old = md("old-doc", "Old")
    forgejo.commit("kb-all", {"guides/old.md": old})
    args = {"path": "guides/old.md", "id": "old-doc", "base_blob": blob_sha(old)}
    res = await call(mcp_client, args)
    assert not res.is_error, res.text
    assert forgejo.files("kb-all", res.data["branch"])["guides/old.md"].startswith('---\nid: "old-doc"')
    assert forgejo.files("kb-all")["guides/old.md"] == old


async def test_stale_base_blob(mcp_client, seeded_db, forgejo):
    old = md("old-doc", "Old")
    forgejo.commit("kb-all", {"guides/old.md": old})
    forgejo.commit("kb-all", {"guides/old.md": md("old-doc", "Newer")})
    res = await call(mcp_client, {"path": "guides/old.md", "id": "old-doc", "base_blob": blob_sha(old)})
    assert res.is_error and res.text.endswith(BLOB_CHANGED)
    assert not forgejo.repos["kb-all"].pulls


async def test_base_blob_for_a_missing_path(mcp_client, seeded_db, forgejo):
    res = await call(mcp_client, {"base_blob": blob_sha("x")})
    assert res.is_error and res.text.endswith(BLOB_CHANGED)


async def test_path_exists(mcp_client, seeded_db, forgejo):
    forgejo.commit("kb-all", {"guides/new.md": md("other")})
    res = await call(mcp_client)
    assert res.is_error and res.text.endswith(PATH_EXISTS)
    assert not forgejo.repos["kb-all"].pulls


async def test_id_in_use(mcp_client, seeded_db, forgejo):
    res = await call(mcp_client, {"id": "doc-all"})  # the seed holds doc-all at all.md
    assert res.is_error and res.text.endswith(ID_IN_USE)
    assert not forgejo.repos["kb-all"].pulls


async def test_id_of_the_same_path_is_an_edit(mcp_client, seeded_db, forgejo):
    old = md("doc-all", "Doc all")
    forgejo.commit("kb-all", {"all.md": old})
    res = await call(mcp_client, {"path": "all.md", "id": "doc-all", "base_blob": blob_sha(old)})
    assert not res.is_error, res.text


async def test_cap_reached(mcp_client, seeded_db, forgejo, deps):
    deps.settings = deps.settings.model_copy(update={"propose_max_open": 2})
    for i in range(2):
        res = await call(mcp_client, {"path": f"p{i}.md", "id": f"p{i}"})
        assert not res.is_error, res.text
    res = await call(mcp_client, {"path": "p9.md", "id": "p9"})
    assert res.is_error and res.text.endswith(TOO_MANY_OPEN)
    assert len(forgejo.repos["kb-all"].pulls) == 2


async def test_other_authors_pulls_do_not_count(mcp_client, seeded_db, forgejo, deps):
    deps.settings = deps.settings.model_copy(update={"propose_max_open": 1})
    forgejo.login = "someone-else"
    assert not (await call(mcp_client, {"path": "p0.md", "id": "p0"})).is_error
    forgejo.login = "kh-bot"
    res = await call(mcp_client, {"path": "p1.md", "id": "p1"})
    assert not res.is_error, res.text


async def test_caller_lacks_the_group(mcp_client, seeded_db, forgejo):
    for group, who in (("hr", alice_all), ("all", dave_none), ("nosuch", alice_all)):
        res = await call(mcp_client, {"group": group}, who)
        assert res.is_error and res.text.endswith(AUDIENCE_NOT_HELD), group
    assert not forgejo.requests


async def test_not_configured(mcp_client, seeded_db, deps):
    deps.forgejo_bot = None
    res = await call(mcp_client)
    assert res.is_error and res.text.endswith(NOT_CONFIGURED)


@pytest.mark.parametrize(
    ("args", "name"),
    [
        ({"path": "../x.md"}, "path"), ({"path": "README.md"}, "path"), ({"path": "a.txt"}, "path"),
        ({"id": "Bad Id"}, "id"), ({"id": ""}, "id"),
        ({"review_by": "2027-02-30"}, "review_by"), ({"review_by": "tomorrow"}, "review_by"),
        ({"title": ""}, "title"), ({"title": "two\nlines"}, "title"),
    ],
)
async def test_bad_args(mcp_client, seeded_db, forgejo, args, name):
    res = await call(mcp_client, args)
    assert res.is_error and res.text.endswith(BAD_ARG.format(name=name))
    assert not forgejo.requests


@pytest.mark.parametrize(
    "args",
    [{"body": "x <!-- hidden --> y"}, {"body": "<script>x</script>"}, {"body": "a\x00b"},
     {"body": "a‮b"}, {"title": "T <b>"}, {"summary": "<img src=x>"}],
)
async def test_bad_text(mcp_client, seeded_db, forgejo, args):
    res = await call(mcp_client, args)
    assert res.is_error and res.text.endswith(BAD_TEXT)
    assert not forgejo.requests


async def test_unknown_label(mcp_client, seeded_db, forgejo):
    for labels in (["nope"], ["ember", "nope"], ["Bad Label"]):
        res = await call(mcp_client, {"labels": labels})
        assert res.is_error and res.text.endswith(BAD_LABEL), labels
    assert not forgejo.requests


async def test_too_large(mcp_client, seeded_db, forgejo, deps):
    deps.settings = deps.settings.model_copy(update={"propose_max_bytes": 200})
    res = await call(mcp_client, {"body": "x" * 300})
    assert res.is_error and res.text.endswith(TOO_LARGE)
    assert not forgejo.requests


async def test_forgejo_failure_hides_the_detail(mcp_client, seeded_db, forgejo):
    forgejo.fail_next("/pulls", 500)
    res = await call(mcp_client)
    assert res.is_error and res.text.endswith(PROPOSE_FAILED)
    assert "500" not in res.text and "/pulls" not in res.text


async def test_forgejo_failure_on_open_pull(mcp_client, seeded_db, forgejo):
    forgejo.fail_next("/branches", 500)
    res = await call(mcp_client)
    assert res.is_error and res.text.endswith(PROPOSE_FAILED)


async def test_a_branch_outside_bot_is_impossible(forgejo, deps):
    bot = deps.forgejo_bot
    head = forgejo.head("kb-all")
    for name in ("main", "feature/x", "bot/", "bot/../main"):
        with pytest.raises(ForgejoRefused):
            await bot.create_branch("kb-all", name, head)
        with pytest.raises(ForgejoRefused):
            await bot.put_file("kb-all", name, "a.md", "x", "m", None)
        with pytest.raises(ForgejoRefused):
            await bot.open_pull("kb-all", name, "main", "t", "b")
    with pytest.raises(ForgejoRefused):
        await bot.open_pull("kb-all", "bot/x", "develop", "t", "b")
    assert not forgejo.requests


def test_error_texts():
    assert errors.PROPOSE_FAILED == "could not open the pull request"


async def test_supersedes_is_written_to_the_file_and_the_pr_body(mcp_client, seeded_db, forgejo):
    res = await call(mcp_client, {"supersedes": ["doc-all"]})
    assert not res.is_error, res.text
    text = forgejo.files("kb-all", res.data["branch"])["guides/new.md"]
    assert 'labels: []\nsupersedes: ["doc-all"]\n---\n' in text
    (pull,) = forgejo.repos["kb-all"].pulls
    assert pull["body"].endswith("Written-by: kh-bot\n\nSupersedes: `doc-all`")


async def test_unknown_supersedes_id_is_bad_arg(mcp_client, seeded_db, forgejo):
    res = await call(mcp_client, {"supersedes": ["doc-all", "no-such-doc"]})
    assert res.is_error and res.text.endswith(BAD_ARG.format(name="supersedes"))
    assert not forgejo.repos["kb-all"].pulls


@pytest.mark.parametrize(
    "supersedes",
    [["doc-all", "doc-all"], ["new-guide"], ["Bad Id"], [f"d{i}" for i in range(21)]],
)
async def test_supersedes_duplicate_own_id_and_too_many_are_bad_arg(
    mcp_client, seeded_db, forgejo, supersedes
):
    res = await call(mcp_client, {"supersedes": supersedes})
    assert res.is_error and res.text.endswith(BAD_ARG.format(name="supersedes"))
    assert not forgejo.repos["kb-all"].pulls


async def test_supersedes_defaults_leave_existing_behaviour(mcp_client, seeded_db, forgejo):
    res = await call(mcp_client)
    assert not res.is_error, res.text
    assert "supersedes" not in forgejo.files("kb-all", res.data["branch"])["guides/new.md"]
    assert "Supersedes" not in forgejo.repos["kb-all"].pulls[0]["body"]
