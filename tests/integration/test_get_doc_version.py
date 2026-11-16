"""``get_doc`` with ``version=<sha>`` (plan section 3.6)."""

from __future__ import annotations

import psycopg

from knowledge_hub.config import parse_config
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.indexer.sync import sync_repo
from tests.support.forgejo_fake import ForgejoFake
from tests.support.principals import alice_all
from tests.unit.test_sync_plan import doc, merged, repo_of

DB = "kh_fresh"
CFG = parse_config(
    {
        "repos": {
            "canonical-all": {"tier": "T0", "audience": "all", "trust_anchor": "a0"},
            "canonical-mgmt": {"tier": "T0", "audience": "mgmt", "trust_anchor": "a0"},
        }
    }
)
V1, V2 = doc("a", title="First"), doc("a", title="Second")


async def indexed(fresh_db, indexer_pool, deps):
    repos = {
        "canonical-all": repo_of([merged("m1", "a0", {"a.md": V1}, 1), merged("m2", "m1", {"a.md": V2}, 2)]),
        "canonical-mgmt": repo_of([merged("n1", "a0", {"s.md": doc("s")}, 1)]),
    }
    fake = ForgejoFake(repos)
    for name in repos:
        await sync_repo(indexer_pool, fake, CFG, name, FakeEmbedder())
    # Index the older commit as a version too (the sync only records the commit that last changed a file).
    with psycopg.connect(fresh_db.admin_dsn(DB), autocommit=True) as conn:
        ids = dict(conn.execute("SELECT doc_key, id FROM kh.documents").fetchall())
        conn.execute(
            "INSERT INTO kh.document_versions (document_id, commit_sha, path, tier, status, stamp)"
            " VALUES (%s, 'm1', 'a.md', 'T0', 'current', 'owner_merge')",
            (ids["a"],),
        )
    deps.config, deps.forgejo = CFG, fake
    return ids, fake


async def test_indexed_older_sha_returns_that_content(fresh_db, indexer_pool, deps, mcp_client):
    ids, fake = await indexed(fresh_db, indexer_pool, deps)
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{ids['a']}", "version": "m1"})
    assert not res.is_error, res.text
    assert res.data["text"] == V1
    assert res.data["commit_sha"] == "m1"
    assert res.data["stamp"] == "owner_merge"
    current = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{ids['a']}"})
    assert current.data["commit_sha"] == "m2" and current.data["text"].startswith("Body of a")


async def test_unknown_sha_is_the_fixed_error(fresh_db, indexer_pool, deps, mcp_client):
    ids, fake = await indexed(fresh_db, indexer_pool, deps)
    fake.calls.clear()
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{ids['a']}", "version": "deadbeef"})
    assert res.is_error
    assert "version deadbeef is not an indexed version of this document" in res.text
    assert "get_raw" not in fake.calls


async def test_hidden_document_is_not_found_without_fetching(fresh_db, indexer_pool, deps, mcp_client):
    ids, fake = await indexed(fresh_db, indexer_pool, deps)
    fake.calls.clear()
    res = await mcp_client.call(alice_all, "get_doc", {"id": f"doc:{ids['s']}", "version": "n1"})
    assert res.is_error and res.text.endswith("not found")
    assert fake.calls == []
