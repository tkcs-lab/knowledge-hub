"""The similarity floor of the search query: the distance bound and its bind parameter."""

from __future__ import annotations

import datetime as dt

import pytest

from knowledge_hub.search.sql import SearchFilters, build_search_query, overdue_weight


def _filters(**kw) -> SearchFilters:
    return SearchFilters(query="q", query_vector=[0.0], embedding_model="fake", **kw)


def test_max_distance_is_the_whole_range_when_the_floor_is_off():
    assert _filters().max_distance() == 2.0


def test_max_distance_follows_the_floor():
    assert _filters(min_similarity=0.4).max_distance() == pytest.approx(0.6)


def test_build_search_query_binds_max_dist():
    sql, params = build_search_query(_filters())
    assert params["max_dist"] == 2.0
    assert sql.count("v.dist <= %(max_dist)s::float8") == 2
    _, params = build_search_query(_filters(min_similarity=0.4))
    assert params["max_dist"] == pytest.approx(0.6)


def test_null_query_vector_binds_null_and_guards_both_vector_lists():
    sql, params = build_search_query(
        SearchFilters(query="q", query_vector=None, embedding_model="fake")
    )
    assert params["qvec"] is None
    assert sql.count("%(qvec)s::vector IS NOT NULL") == 2


def test_query_vector_is_bound_as_a_bracketed_string():
    _, params = build_search_query(_filters())
    assert params["qvec"].startswith("[")


def test_overdue_weight_is_one_when_not_overdue_or_off():
    assert overdue_weight(0, 180, 0.5) == 1.0
    assert overdue_weight(-5, 180, 0.5) == 1.0
    assert overdue_weight(400, 0, 0.5) == 1.0


def test_overdue_weight_values():
    assert overdue_weight(1, 180, 0.5) == pytest.approx(0.99808, abs=1e-5)
    assert overdue_weight(180, 180, 0.5) == pytest.approx(0.75)
    assert overdue_weight(400, 180, 0.5) == pytest.approx(0.60716, abs=1e-5)


def test_overdue_weight_never_below_the_floor():
    assert overdue_weight(10**6, 1, 0.3) >= 0.3
    assert overdue_weight(10**6, 1, 0.3) == pytest.approx(0.3)


def test_build_search_query_binds_overdue_params():
    sql, params = build_search_query(_filters())
    assert params["half_life"] == 0
    assert params["overdue_floor"] == 0.5
    assert isinstance(params["as_of"], dt.date)
    day = dt.date(2026, 6, 1)
    _, params = build_search_query(_filters(overdue_half_life_days=180, overdue_floor=0.2, as_of=day))
    assert (params["half_life"], params["overdue_floor"], params["as_of"]) == (180, 0.2, day)
    assert "LEFT JOIN kh.documents od ON od.id = c.document_id" in sql
    assert "WHEN %(half_life)s::int > 0 AND c.tier = 'T0' AND od.review_by IS NOT NULL" in sql
