import base64
import json

import pytest

from knowledge_hub.forgejo import ForgejoBot, ForgejoError, ForgejoRefused
from tests.support.forgejo_fake import FakeForgejo, blob_sha


@pytest.fixture
def fake():
    f = FakeForgejo(org="kh", token="secret-token", login="kh-bot")
    f.add_repo("kb-all")
    f.commit("kb-all", {"a.md": "one"})
    return f


@pytest.fixture
async def bot(fake):
    b = ForgejoBot("https://forgejo.test", "secret-token", "kh", transport=fake.transport)
    yield b
    await b.aclose()


async def test_head_sha(fake, bot):
    assert await bot.head_sha("kb-all", "main") == fake.head("kb-all")


async def test_blob_sha(fake, bot):
    assert await bot.blob_sha("kb-all", "a.md", "main") == blob_sha("one")
    assert await bot.blob_sha("kb-all", "a.md", fake.head("kb-all")) == blob_sha("one")
    assert await bot.blob_sha("kb-all", "nope.md", "main") is None


async def test_blob_sha_server_error(fake, bot):
    fake.fail_next("contents", 500)
    with pytest.raises(ForgejoError, match="HTTP 500"):
        await bot.blob_sha("kb-all", "a.md", "main")


async def test_create_branch_and_put_new_file(fake, bot):
    head = await bot.head_sha("kb-all", "main")
    await bot.create_branch("kb-all", "bot/propose/x", head)
    assert fake.head("kb-all", "bot/propose/x") == head
    commit = await bot.put_file("kb-all", "bot/propose/x", "n/new.md", "new", "msg", None)
    assert commit["parents"] == [head] and commit["sha"] == fake.head("kb-all", "bot/propose/x")
    assert fake.files("kb-all", "bot/propose/x")["n/new.md"] == "new"
    assert "n/new.md" not in fake.files("kb-all", "main")
    sent = fake.bodies[-1]
    assert "sha" not in sent and sent["branch"] == "bot/propose/x" and sent["message"] == "msg"
    assert base64.b64decode(sent["content"]) == b"new"
    assert fake.requests[-1][0] == "POST"


async def test_put_file_existing_needs_prev_blob(fake, bot):
    await bot.create_branch("kb-all", "bot/propose/x", fake.head("kb-all"))
    with pytest.raises(ForgejoError, match="HTTP 422"):
        await bot.put_file("kb-all", "bot/propose/x", "a.md", "two", "m", None)
    await bot.put_file("kb-all", "bot/propose/x", "a.md", "two", "m", blob_sha("one"))
    assert fake.files("kb-all", "bot/propose/x")["a.md"] == "two"
    assert fake.bodies[-1]["sha"] == blob_sha("one")
    assert fake.requests[-1][0] == "PUT"


async def test_put_file_stale_prev_blob_conflicts(fake, bot):
    await bot.create_branch("kb-all", "bot/propose/x", fake.head("kb-all"))
    with pytest.raises(ForgejoError, match="HTTP 409"):
        await bot.put_file("kb-all", "bot/propose/x", "a.md", "two", "m", "0" * 40)


async def test_create_branch_twice_fails(fake, bot):
    await bot.create_branch("kb-all", "bot/b", fake.head("kb-all"))
    with pytest.raises(ForgejoError, match="HTTP 409"):
        await bot.create_branch("kb-all", "bot/b", fake.head("kb-all"))


async def test_open_pull_and_list(fake, bot):
    await bot.create_branch("kb-all", "bot/propose/x", fake.head("kb-all"))
    await bot.put_file("kb-all", "bot/propose/x", "n.md", "n", "m", None)
    pull = await bot.open_pull("kb-all", "bot/propose/x", "main", "Propose: n", "body")
    assert pull["number"] == 1 and pull["html_url"].endswith("/pulls/1")
    assert pull["head"]["ref"] == "bot/propose/x" and pull["base"]["ref"] == "main"
    assert fake.bodies[-1] == {"head": "bot/propose/x", "base": "main", "title": "Propose: n", "body": "body"}
    assert [p["number"] for p in await bot.list_open_pulls("kb-all", "kh-bot")] == [1]
    assert await bot.list_open_pulls("kb-all", "someone-else") == []


async def test_list_open_pulls_pages_and_skips_closed(fake, bot, monkeypatch):
    import knowledge_hub.forgejo.bot as mod

    monkeypatch.setattr(mod, "PAGE", 2)
    for i in range(5):
        await bot.create_branch("kb-all", f"bot/b{i}", fake.head("kb-all"))
        await bot.open_pull("kb-all", f"bot/b{i}", "main", "t", "")
    fake.repos["kb-all"].pulls[0]["state"] = "closed"
    pulls = await bot.list_open_pulls("kb-all", "kh-bot")
    assert [p["number"] for p in pulls] == [2, 3, 4, 5]
    assert sum(1 for m, p in fake.requests if m == "GET" and p.endswith("/pulls")) == 3


async def test_open_pull_unknown_head(bot):
    with pytest.raises(ForgejoError, match="HTTP 404"):
        await bot.open_pull("kb-all", "bot/missing", "main", "t", "")


# -- guards: nothing is sent --------------------------------------------------------------------

BAD_BRANCHES = ["main", "bot/", "bot", "feature/x", "xbot/x", "bot/../main", "bot//x", "bot/a/../b", "", None]


@pytest.mark.parametrize("name", BAD_BRANCHES)
async def test_create_branch_guard(fake, bot, name):
    n = len(fake.requests)
    with pytest.raises(ForgejoRefused):
        await bot.create_branch("kb-all", name, fake.head("kb-all"))
    assert len(fake.requests) == n


@pytest.mark.parametrize("name", BAD_BRANCHES)
async def test_put_file_guard(fake, bot, name):
    n = len(fake.requests)
    with pytest.raises(ForgejoRefused):
        await bot.put_file("kb-all", name, "a.md", "x", "m", None)
    assert len(fake.requests) == n


@pytest.mark.parametrize("name", BAD_BRANCHES)
async def test_open_pull_head_guard(fake, bot, name):
    n = len(fake.requests)
    with pytest.raises(ForgejoRefused):
        await bot.open_pull("kb-all", name, "main", "t", "")
    assert len(fake.requests) == n


@pytest.mark.parametrize("base", ["develop", "bot/x", "", "Main"])
async def test_open_pull_base_guard(fake, bot, base):
    n = len(fake.requests)
    with pytest.raises(ForgejoRefused, match="base must be main"):
        await bot.open_pull("kb-all", "bot/x", base, "t", "")
    assert len(fake.requests) == n


def test_refused_is_a_forgejo_error():
    assert issubclass(ForgejoRefused, ForgejoError)


async def test_errors_never_leak_token_or_body(fake, bot):
    fake.fail_next("branches", 500)
    with pytest.raises(ForgejoError) as exc:
        await bot.create_branch("kb-all", "bot/x", fake.head("kb-all"))
    assert str(exc.value) == "POST /repos/kh/kb-all/branches failed: HTTP 500"
    assert "secret-token" not in str(exc.value) and "injected" not in str(exc.value)
    assert json.loads(json.dumps(fake.bodies[-1]))["new_branch_name"] == "bot/x"
