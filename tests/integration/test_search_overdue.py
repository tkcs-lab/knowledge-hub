"""search: the graduated overdue weight of T0 hits (plan 3.1)."""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.server import Deps
from knowledge_hub.search.sql import overdue_weight
from tests.support.bearer import TestBearerResolver
from tests.support.pg import FRESH_DB
from tests.support.principals import alice_all
from tests.support.seed import add_chunked_doc, add_chunked_record

pytestmark = pytest.mark.integration

AS_OF = date(2026, 6, 1)


@pytest.fixture
def cursor_key(tmp_path):
    path = tmp_path / "cursor.key"
    path.write_bytes(b"k" * 32)
    return path


@pytest.fixture
def app_settings(db_settings, cursor_key):
    from tests.support.mcp_client import HOST

    update = {"env": "test", "allowed_hosts": HOST, "cursor_key_file": cursor_key}
    return db_settings.model_copy(update=update)


@pytest.fixture
def deps(app_settings, kh_config, app_pool, indexer_pool) -> Deps:
    return Deps(
        settings=app_settings, config=kh_config, app_pool=app_pool, indexer_pool=indexer_pool,
        embedder=FakeEmbedder(), bearer=TestBearerResolver(kh_config),
    )


@pytest.fixture
def db(seeded_db, fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB)) as conn:
        yield conn
        conn.commit()


@pytest.fixture
def as_of(monkeypatch):
    monkeypatch.setattr("knowledge_hub.tools.search._today", lambda: AS_OF)
    return AS_OF


async def hits(mcp_client, **args):
    res = await mcp_client.call(alice_all, "search", args)
    assert not res.is_error, res.text
    return res.data["hits"]


def ago(days: int) -> date:
    return AS_OF - timedelta(days=days)


async def score(mcp_client, app_settings, key, half_life, floor=0.5, **args):
    """The score of hit ``key`` for ``query`` at the given overdue settings."""
    app_settings.review_overdue_half_life_days = half_life
    app_settings.review_overdue_floor = floor
    out = await hits(mcp_client, **args)
    return next(h for h in out if h["id"] == key)


async def test_fresh_beats_one_day_and_400_days_overdue(mcp_client, db, app_settings, as_of):
    old = add_chunked_doc(db, "alpha beta", review_by=ago(400), stale=False)
    day = add_chunked_doc(db, "alpha beta gamma", review_by=ago(1), stale=False)
    fresh = add_chunked_doc(db, "alpha beta gamma delta epsilon", review_by=AS_OF + timedelta(days=30),
                            stale=False)
    db.commit()
    app_settings.review_overdue_half_life_days = 1
    app_settings.review_overdue_floor = 0.2
    out = await hits(mcp_client, query="alpha beta")
    assert [h["id"] for h in out] == [f"doc:{fresh}", f"doc:{day}", f"doc:{old}"]


async def test_weight_follows_days_overdue_at_recommended_values(mcp_client, db, app_settings, as_of):
    docs = {d: add_chunked_doc(db, f"quokka {d}", review_by=ago(d), stale=False) for d in (1, 400)}
    db.commit()
    for days, doc in docs.items():
        key = f"doc:{doc}"
        base = await score(mcp_client, app_settings, key, 0, query=f"quokka {days}")
        graded = await score(mcp_client, app_settings, key, 180, 0.5, query=f"quokka {days}")
        assert graded["score"] / base["score"] == pytest.approx(overdue_weight(days, 180, 0.5), rel=1e-9)


async def test_floor_is_respected_for_very_old_documents(mcp_client, db, app_settings, as_of):
    doc = add_chunked_doc(db, "numbat", review_by=date(1900, 1, 1), stale=False)
    db.commit()
    key = f"doc:{doc}"
    base = await score(mcp_client, app_settings, key, 0, query="numbat")
    graded = await score(mcp_client, app_settings, key, 1, 0.3, query="numbat")
    ratio = graded["score"] / base["score"]
    assert ratio == pytest.approx(0.3)
    assert ratio >= 0.3


async def test_not_yet_flagged_document_is_weighted_without_kh_daily(mcp_client, db, app_settings, as_of):
    doc = add_chunked_doc(db, "dingo", review_by=ago(400), stale=False)
    db.commit()
    key = f"doc:{doc}"
    base = await score(mcp_client, app_settings, key, 0, query="dingo")
    graded = await score(mcp_client, app_settings, key, 180, 0.5, query="dingo")
    assert graded["score"] / base["score"] == pytest.approx(overdue_weight(400, 180, 0.5), rel=1e-9)
    assert base["stale"] is False and graded["stale"] is False


async def test_t0a_and_t3_scores_are_unchanged(mcp_client, db, app_settings, as_of):
    tech = add_chunked_doc(db, "wallaby tech", tier="T0a", review_by=ago(400), stale=True)
    rec = add_chunked_record(db, "wallaby record")
    db.commit()
    for key in (f"doc:{tech}", f"rec:{rec}"):
        base = await score(mcp_client, app_settings, key, 0, query="wallaby")
        graded = await score(mcp_client, app_settings, key, 180, 0.2, query="wallaby")
        assert graded["score"] == base["score"]


async def test_hidden_hits_keep_the_flat_stale_weight(mcp_client, db, app_settings, as_of):
    new = add_chunked_doc(db, "unrelated successor words", embed=False)
    old = add_chunked_doc(db, "echidna", superseded_by=new, review_by=ago(400), stale=False)
    db.commit()
    key = f"doc:{old}"
    base = await score(mcp_client, app_settings, key, 0, query="echidna", include_outdated=True)
    graded = await score(mcp_client, app_settings, key, 180, 0.5, query="echidna", include_outdated=True)
    assert graded["score"] == base["score"]


async def test_half_life_zero_matches_the_stale_flag_exactly(mcp_client, db, app_settings, as_of):
    stale = add_chunked_doc(db, "kookaburra", review_by=ago(400), stale=True)
    add_chunked_doc(db, "kookaburra laughs loudly today", review_by=AS_OF + timedelta(days=30), stale=False)
    db.commit()
    key = f"doc:{stale}"
    weight = app_settings.stale_weight
    flagged = await score(mcp_client, app_settings, key, 0, query="kookaburra")
    app_settings.stale_weight = 1.0
    plain = await score(mcp_client, app_settings, key, 0, query="kookaburra")
    assert flagged["score"] == weight * plain["score"]
