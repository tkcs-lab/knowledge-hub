"""Search ranking: tier, freshness, supersession, disputes and recall under row-level security."""

import uuid
from datetime import UTC, datetime

import pytest

from knowledge_hub.db.caller_tx import caller_tx
from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.server import Deps
from tests.support.principals import alice_all, carol_hr, wf_token

QUERY = "budget forecast review"
EMBEDDER = FakeEmbedder()
NORMATIVE_PREFIX = "cannot answer normatively from T2/T3 only"


_APP_POOLS: dict[int, object] = {}


def app_pool_of(indexer_pool):
    """The app pool that goes with an indexer pool (registered by the ``deps`` fixture)."""
    return _APP_POOLS[id(indexer_pool)]


@pytest.fixture
def app_settings(db_settings, tmp_path):
    from tests.support.mcp_client import HOST

    key = tmp_path / "cursor.key"
    key.write_bytes(b"k" * 32)
    return db_settings.model_copy(update={"env": "test", "allowed_hosts": HOST, "cursor_key_file": key})


@pytest.fixture
def deps(app_settings, app_pool, indexer_pool):
    from knowledge_hub.config import Config

    _APP_POOLS[id(indexer_pool)] = app_pool
    return Deps(
        settings=app_settings,
        config=Config(),
        app_pool=app_pool,
        indexer_pool=indexer_pool,
        embedder=EMBEDDER,
    )


async def _chunk(conn, owner_col: str, owner_id, text: str, *, embed: bool = True) -> None:
    (vec,) = await EMBEDDER.embed([text])
    literal = "[" + ",".join(repr(x) for x in vec) + "]" if embed else None
    await conn.execute(
        f"INSERT INTO kh.chunks (id, {owner_col}, ordinal, heading_path, text, content_sha, embedding,"
        " embedding_model, owner_key, tier, required_audiences)"
        " VALUES (%s, %s, 0, 'H', %s, %s, %s::vector, %s, '', 'T0', '{}')",
        (uuid.uuid4(), owner_id, text, uuid.uuid4().hex, literal, EMBEDDER.model if embed else None),
    )


async def add_doc(
    pool,
    text: str,
    *,
    tier: str = "T0",
    status: str = "current",
    stale: bool = False,
    audiences: tuple[str, ...] = ("all",),
    emails: list[str] | None = None,
    domain: str = "product",
    entities: tuple[str, ...] = (),
    embed: bool = True,
) -> str:
    """A document with one chunk, written as ``kh_indexer``; returns its citation id."""
    doc = uuid.uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO kh.documents (id, repo, path, doc_key, tier, domain, title, body, status, stale,"
            " owner, entities, commit_sha, content_sha, approved_by, approved_at, review_by,"
            " stamp, required_audiences, allowed_emails)"
            " VALUES (%s, 'canonical-all', %s, %s, %s, %s, 'T', %s, %s, %s, 'team', %s, 'c1', %s,"
            " '{x}', now(), '2027-01-01', 'reviewed', %s, %s)",
            (doc, f"p/{doc}.md", str(doc), tier, domain, text, status, stale, list(entities),
             uuid.uuid4().hex, list(audiences), emails),
        )
        await _chunk(conn, "document_id", doc, text, embed=embed)
    return f"doc:{doc}"


async def add_raw(
    pool,
    text: str,
    *,
    audiences: tuple[str, ...] = ("all",),
    emails: list[str] | None = None,
    ingested_at: datetime | None = None,
    domain: str = "technical",
) -> str:
    raw = uuid.uuid4()
    async with caller_tx(app_pool_of(pool), wf_token) as conn:
        await conn.execute(
            "INSERT INTO kh.raw_records (id, source_system, source_ref, source_url, participants, title,"
            " excerpt, ingested_by_sub, ingested_by_type, ingested_at, domain)"
            " VALUES (%s, 'meet', %s, %s, '{p@example.org}', 'R', %s, %s, 'n8n_workflow', %s, %s)",
            (raw, str(raw), f"https://meet.example/{raw}", text, wf_token.sub,
             ingested_at or datetime.now(UTC), domain),
        )
    async with pool.connection() as conn:
        # The insert trigger assigned participant visibility; set the wanted one.
        await conn.execute(
            "UPDATE kh.raw_records SET required_audiences = %s, allowed_emails = %s WHERE id = %s",
            (list(audiences), emails, raw),
        )
        await _chunk(conn, "raw_id", raw, text)
    return f"raw:{raw}"


async def add_derived(
    pool,
    text: str,
    *,
    audiences: tuple[str, ...] = ("all",),
    emails: list[str] | None = None,
    created_at: datetime | None = None,
    domain: str = "technical",
) -> str:
    der, src = uuid.uuid4(), uuid.uuid4()
    async with pool.connection() as conn:  # the source document carries the visibility (plan 3.7)
        await conn.execute(
            "INSERT INTO kh.documents (id, repo, path, doc_key, tier, domain, title, body, status,"
            " commit_sha, content_sha, required_audiences, allowed_emails)"
            " VALUES (%s, 'canonical-all', %s, %s, 'T0', 'product', 'S', 's', 'current', 'c1', %s, %s, %s)",
            (src, f"s/{src}.md", str(src), uuid.uuid4().hex, list(audiences), emails),
        )
    async with caller_tx(app_pool_of(pool), wf_token) as conn:
        await conn.execute(
            "INSERT INTO kh.derived_records (id, kind, title, body, domain, created_by_sub, created_by_type,"
            " created_at, required_audiences, allowed_emails)"
            " VALUES (%s, 'summary', 'D', %s, %s, %s, 'n8n_workflow', %s, %s, %s)",
            (der, text, domain, wf_token.sub, created_at or datetime.now(UTC), list(audiences), emails),
        )
        await conn.execute(
            "INSERT INTO kh.derived_sources (derived_id, source_kind, source_id, src_required_audiences)"
            " VALUES (%s, 'doc', %s, '{}')",
            (der, str(src)),
        )
    async with pool.connection() as conn:
        await _chunk(conn, "derived_id", der, text)
    return f"derived:{der}"


async def search(client, caller, query: str = QUERY, **args):
    return await client.call(caller, "search", {"query": query, **args})


def ids(res) -> list[str]:
    return [h["citation_id"] for h in res.data["hits"]]


async def test_t0_ranks_above_equally_relevant_t3(indexer_pool, mcp_client):
    t3 = await add_raw(indexer_pool, QUERY)
    t0 = await add_doc(indexer_pool, QUERY)
    res = await search(mcp_client, alice_all, min_tier="T3")
    assert not res.is_error, res.text
    assert ids(res) == [t0, t3]
    assert [h["tier"] for h in res.data["hits"]] == ["T0", "T3"]
    assert [h["stamp"] for h in res.data["hits"]] == ["reviewed", ""]


async def test_stale_ranks_below_current(indexer_pool, mcp_client):
    stale = await add_doc(indexer_pool, QUERY, stale=True)
    current = await add_doc(indexer_pool, QUERY)
    res = await search(mcp_client, alice_all)
    assert ids(res) == [current, stale]
    assert res.data["hits"][1]["stale"] is True


async def test_superseded_ranks_below_current(indexer_pool, mcp_client):
    old = await add_doc(indexer_pool, QUERY, status="superseded")
    current = await add_doc(indexer_pool, QUERY)
    res = await search(mcp_client, alice_all)
    assert ids(res) == [current, old]
    assert res.data["hits"][1]["status"] == "superseded"


async def test_disputed_is_down_weighted(indexer_pool, mcp_client):
    disputed = await add_doc(indexer_pool, QUERY, status="disputed")
    current = await add_doc(indexer_pool, QUERY)
    res = await search(mcp_client, alice_all)
    assert ids(res) == [current, disputed]


async def test_only_disputed_t0_with_visible_t3_hits_is_not_normative(indexer_pool, mcp_client):
    await add_doc(indexer_pool, QUERY, status="disputed")
    await add_raw(indexer_pool, QUERY)
    await add_derived(indexer_pool, QUERY)
    for tier in ("T0", "T1"):
        res = await search(mcp_client, alice_all, min_tier=tier)
        assert res.is_error
        assert res.text.endswith("cannot answer normatively from T2/T3 only: 2 lower-tier hits; "
                                 'call search with min_tier="T3" to see them'), res.text
    # Asking for every tier is not a normative question; the disputed hit is still shown.
    wide = await search(mcp_client, alice_all, min_tier="T3")
    assert not wide.is_error and len(wide.data["hits"]) == 3
    # A current T0 hit makes the answer normative again.
    await add_doc(indexer_pool, QUERY)
    ok = await search(mcp_client, alice_all, min_tier="T0")
    assert not ok.is_error and ok.data["lower_tier_hits"]["T3"] == 1 and ok.data["lower_tier_hits"]["T2"] == 1


async def test_no_hits_and_no_lower_tier_hits_is_an_empty_answer(indexer_pool, mcp_client):
    res = await search(mcp_client, alice_all, "zzz nothing", min_tier="T0")
    assert not res.is_error
    assert res.data["hits"] == []
    assert res.data["lower_tier_hits"]["T2"] == 0 and res.data["lower_tier_hits"]["T3"] == 0


async def test_narrow_audience_still_gets_its_best_chunks(indexer_pool, mcp_client, app_settings):
    pool = app_settings.search_pool
    for _ in range(pool + 10):  # hidden chunks that match more strongly than anything visible
        await add_doc(indexer_pool, QUERY, audiences=("mgmt",))
    visible = [
        await add_doc(indexer_pool, f"review of the budget forecast number {i}", audiences=("all", "hr"))
        for i in range(3)
    ]
    res = await search(mcp_client, carol_hr)
    assert not res.is_error, res.text
    assert set(ids(res)) == set(visible)


async def test_filters_and_hit_fields(indexer_pool, mcp_client):
    a = await add_doc(indexer_pool, QUERY, domain="product", entities=("prd:a",))
    await add_doc(indexer_pool, QUERY, domain="technical", entities=("prd:b",))
    res = await search(mcp_client, alice_all, domains=["product"], entities=["prd:a", "prd:z"])
    assert ids(res) == [a]
    hit = res.data["hits"][0]
    assert hit["text"] == QUERY and hit["owner"] == "team" and hit["approved_by"] == ["x"]
    assert hit["review_by"] == "2027-01-01" and hit["entities"] == ["prd:a"] and hit["status"] == "current"
    assert hit["source_link"] == ""  # no forgejo_web_url configured


async def test_pagination_walks_the_list_once(indexer_pool, mcp_client):
    made = {await add_doc(indexer_pool, f"{QUERY} variant {i}") for i in range(5)}
    seen: list[str] = []
    cursor = ""
    for _ in range(5):
        res = await search(mcp_client, alice_all, limit=2, cursor=cursor)
        assert not res.is_error, res.text
        seen += ids(res)
        cursor = res.data["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 5 and set(seen) == made


async def test_limit_is_bounded(indexer_pool, mcp_client):
    for i in range(3):
        await add_doc(indexer_pool, f"{QUERY} {i}")
    assert len((await search(mcp_client, alice_all, limit=0)).data["hits"]) == 1
    assert len((await search(mcp_client, alice_all, limit=500)).data["hits"]) == 3


async def test_older_embedding_model_is_not_compared(indexer_pool, mcp_client, app_pool):
    other = await add_doc(indexer_pool, "unrelated words entirely")
    async with indexer_pool.connection() as conn:
        await conn.execute("UPDATE kh.chunks SET embedding_model = 'other'")
    res = await search(mcp_client, alice_all, "something else completely")
    assert other not in ids(res)  # vector list skips it; full text does not match
