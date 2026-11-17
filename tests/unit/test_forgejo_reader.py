import pytest

from knowledge_hub.forgejo import ForgejoError, ForgejoReader
from tests.support.forgejo_fake import FakeForgejo, blob_sha


@pytest.fixture
def fake():
    f = FakeForgejo(org="kh", token="secret-token")
    f.add_repo("kb-all")
    return f


@pytest.fixture
async def reader(fake):
    r = ForgejoReader("https://forgejo.test", "secret-token", "kh", transport=fake.transport)
    yield r
    await r.aclose()


async def test_head_sha(fake, reader):
    sha = fake.commit("kb-all", {"a.md": "x"})
    assert await reader.head_sha("kb-all", "main") == sha


async def test_head_sha_unknown_branch(reader):
    with pytest.raises(ForgejoError, match="HTTP 404"):
        await reader.head_sha("kb-all", "nope")


async def test_list_tree_entries(fake, reader):
    sha = fake.commit("kb-all", {"a.md": "héllo", "docs/b.md": "b"})
    tree = await reader.list_tree("kb-all", sha)
    by_path = {e["path"]: e for e in tree}
    assert set(by_path) == {"a.md", "docs", "docs/b.md"}
    assert by_path["a.md"] == {"path": "a.md", "sha": blob_sha("héllo"), "type": "blob", "size": 6}
    assert by_path["docs"]["type"] == "tree"


async def test_list_tree_pages(fake, reader, monkeypatch):
    import knowledge_hub.forgejo.reader as mod

    monkeypatch.setattr(mod, "TREE_PAGE", 2)
    sha = fake.commit("kb-all", {f"f{i}.md": str(i) for i in range(5)})
    tree = await reader.list_tree("kb-all", sha)
    assert [e["path"] for e in tree] == [f"f{i}.md" for i in range(5)]
    assert sum(1 for m, p in fake.requests if "git/trees" in p) == 3


async def test_get_raw_at_sha_not_head(fake, reader):
    old = fake.commit("kb-all", {"a.md": "one"})
    fake.commit("kb-all", {"a.md": "two"})
    assert await reader.get_raw("kb-all", "a.md", old) == "one"
    assert await reader.get_raw("kb-all", "a.md", fake.head("kb-all")) == "two"


async def test_get_raw_missing(fake, reader):
    with pytest.raises(ForgejoError, match="HTTP 404"):
        await reader.get_raw("kb-all", "nope.md", fake.head("kb-all"))


async def test_is_ancestor(fake, reader):
    first = fake.commit("kb-all", {"a.md": "1"})
    second = fake.commit("kb-all", {"a.md": "2"})
    assert await reader.is_ancestor("kb-all", first, second) is True
    assert await reader.is_ancestor("kb-all", second, second) is True
    assert await reader.is_ancestor("kb-all", second, first) is False


async def test_is_ancestor_after_rewrite(fake, reader):
    old = fake.commit("kb-all", {"a.md": "1"})
    new = fake.rewrite("kb-all", {"a.md": "1"})
    assert await reader.is_ancestor("kb-all", old, new) is False


async def test_is_ancestor_unknown_sha_and_errors_answer_false(fake, reader):
    head = fake.head("kb-all")
    assert await reader.is_ancestor("kb-all", "f" * 40, head) is False
    assert await reader.is_ancestor("kb-missing", head, head) is False
    fake.fail_next("compare", 500)
    assert await reader.is_ancestor("kb-all", head, head) is False


async def test_errors_never_leak_token(fake, reader):
    fake.fail_next("branches", 500)
    with pytest.raises(ForgejoError) as exc:
        await reader.head_sha("kb-all", "main")
    assert str(exc.value) == "GET /repos/kh/kb-all/branches/main failed: HTTP 500"
    assert "secret-token" not in str(exc.value)


async def test_wrong_token_is_401_without_leak():
    fake = FakeForgejo(token="right")
    fake.add_repo("r")
    reader = ForgejoReader("https://forgejo.test", "wrong", "kh", transport=fake.transport)
    with pytest.raises(ForgejoError, match="HTTP 401") as exc:
        await reader.head_sha("r", "main")
    assert "wrong" not in str(exc.value)
    await reader.aclose()


async def test_sends_token_header(fake, reader):
    await reader.head_sha("kb-all", "main")
    assert fake.requests[-1] == ("GET", "/api/v1/repos/kh/kb-all/branches/main")
