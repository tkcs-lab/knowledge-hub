"""The structured query builder (plan 3.6, p6): refusals and statement text."""

from __future__ import annotations

import json

import pytest

from knowledge_hub.datasets.model import Column, Dataset
from knowledge_hub.datasets.query import build_query, parse_spec
from knowledge_hub.mcp import errors
from knowledge_hub.mcp.errors import KhError

DS = Dataset(
    name="deals",
    title="Deals",
    description="",
    audiences=("all",),
    title_column="name",
    history=False,
    max_outdated_percent=20,
    columns=(
        Column("name", "text", "Name", 1, filterable=True),
        Column("stage", "choice", "Stage", 2, choices=("open", "won"), filterable=True),
        Column("amount", "number", "Amount", 3, filterable=True),
        Column("close_date", "date", "Close", 4, filterable=True),
        Column("note", "text", "Note", 5),
    ),
)


def spec(**kw):
    args = dict(
        select=[], where="", group_by=[], metrics=[], time_bucket="", order=[],
        limit=100, include_outdated=False, as_of="", max_rows=1000,
    )  # fmt: skip
    args.update(kw)
    return parse_spec(DS, **args)


def refused(arg, **kw):
    with pytest.raises(KhError) as exc:
        spec(**kw)
    assert exc.value.text == errors.BAD_ARG.format(name=arg)


def where(*conds):
    return json.dumps(list(conds))


def text(**kw):
    s = spec(**kw)
    stmt, params, cols = build_query(s, DS, ["all"])
    return stmt.as_string(None), params, cols


@pytest.mark.parametrize(
    ("arg", "kw"),
    [
        ("where", {"where": where({"column": "note", "op": "eq", "value": "x"})}),
        ("group_by", {"group_by": ["note"]}),
        ("time_bucket", {"time_bucket": "note:month"}),
        ("time_bucket", {"time_bucket": "name:month"}),
        ("time_bucket", {"time_bucket": "close_date:decade"}),
        ("metrics", {"metrics": ["sum:name"]}),
        ("metrics", {"metrics": ["avg:close_date"]}),
        ("metrics", {"metrics": ["bogus:amount"]}),
        ("where", {"where": where({"column": "amount", "op": "like", "value": 1})}),
        ("where", {"where": where({"column": "stage", "op": "lt", "value": "open"})}),
        ("where", {"where": where({"column": "stage", "op": "eq", "value": "lost"})}),
        ("select", {"select": ["name"], "metrics": ["count"]}),
        ("select", {"select": ["nope"]}),
        ("limit", {"limit": 1001}),
        ("limit", {"limit": 0}),
        ("where", {"where": where(*[{"column": "amount", "op": "gt", "value": i} for i in range(21)])}),
        ("where", {"where": where({"column": "name", "op": "in", "value": [str(i) for i in range(101)]})}),
        ("where", {"where": where({"column": "name", "op": "in", "value": []})}),
        ("where", {"where": "{"}),
        ("order", {"order": ["nope"], "metrics": ["count"]}),
        ("as_of", {"as_of": "yesterday"}),
    ],
)
def test_refusals_name_the_argument(arg, kw):
    refused(arg, **kw)


def test_in_list_of_100_is_accepted():
    s = spec(where=where({"column": "name", "op": "in", "value": [str(i) for i in range(100)]}))
    assert len(s.where[0].value) == 100


def test_20_conditions_are_accepted():
    assert (
        len(spec(where=where(*[{"column": "amount", "op": "gt", "value": i} for i in range(20)])).where) == 20
    )


def test_output_names():
    _, _, cols = text(
        group_by=["stage"],
        metrics=["count", "sum:amount", "count_distinct:name", "avg:amount", "min:close_date", "max:amount"],
        time_bucket="close_date:month",
    )
    assert cols == [
        "stage",
        "bucket",
        "count",
        "sum_amount",
        "count_distinct_name",
        "avg_amount",
        "min_close_date",
        "max_amount",
    ]


def test_default_order_is_bucket_and_descending_prefix():
    stmt, _, _ = text(metrics=["sum:amount"], time_bucket="close_date:week")
    assert 'ORDER BY "bucket" ASC' in stmt
    stmt, _, _ = text(group_by=["stage"], metrics=["sum:amount"], order=["-sum_amount"])
    assert 'ORDER BY "sum_amount" DESC' in stmt
    assert 'GROUP BY "stage"' in stmt


def test_limit_plus_one_and_outdated_filter():
    stmt, params, _ = text(limit=50)
    assert stmt.endswith("LIMIT 51")
    assert 'NOT "_outdated"' in stmt
    assert params == [["all"]]
    stmt, _, _ = text(include_outdated=True)
    assert "_outdated" not in stmt


def test_values_are_parameters_not_text():
    stmt, params, _ = text(where=where({"column": "name", "op": "eq", "value": "x'; drop table t;--"}))
    assert "drop" not in stmt
    assert params[1] == "x'; drop table t;--"


def test_default_row_list_selects_key_and_columns():
    _, _, cols = text()
    assert cols == ["_key", "name", "stage", "amount", "close_date", "note"]


def test_source_updated_at_bucket_uses_utc_and_filter_needs_timezone():
    stmt, _, _ = text(metrics=["count"], time_bucket="_source_updated_at:day")
    assert "AT TIME ZONE 'UTC'" in stmt
    refused(
        "where", where=where({"column": "_source_updated_at", "op": "gt", "value": "2026-01-01T00:00:00"})
    )
    s = spec(where=where({"column": "_source_updated_at", "op": "gt", "value": "2026-01-01T00:00:00Z"}))
    assert s.where[0].column == "_source_updated_at"


def test_as_of_is_parsed_but_ignored():
    s = spec(as_of="2026-01-01T00:00:00Z")
    assert s.as_of is not None
    assert build_query(s, DS, ["all"])[0].as_string(None) == text()[0]
