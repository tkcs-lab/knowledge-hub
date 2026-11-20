"""The similarity floor of the search query: the distance bound and its bind parameter."""

from __future__ import annotations

import pytest

from knowledge_hub.search.sql import SearchFilters, build_search_query


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
