"""Parsing and coercing the rows argument of upsert_rows (plan 3.3)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from knowledge_hub.datasets.model import Column, Dataset
from knowledge_hub.datasets.rows import MAX_ROWS, RowError, coerce, parse_rows
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
        Column("name", "text", "Name", 1),
        Column("stage", "choice", "Stage", 2, choices=("open", "won")),
        Column("amount", "number", "Amount", 3, source_name="Amount__c"),
        Column("close_date", "date", "Close", 4),
        Column("won", "bool", "Won", 5),
    ),
)
WHEN = "2026-11-01T10:00:00Z"


def row(key="a", when=WHEN, **fields):
    return {"key": key, "source_updated_at": when, "fields": fields}


def parse(*rows):
    return parse_rows(json.dumps(list(rows)), DS)


def problem(*rows) -> str:
    with pytest.raises(RowError) as exc:
        parse(*rows)
    return f"{exc.value.index}: {exc.value.problem}"


@pytest.mark.parametrize("text", ["{", "{}", "[]", '"x"', '[{"key": "a"}]'])
def test_bad_shape_is_bad_arg_rows(text):
    with pytest.raises(KhError) as exc:
        parse_rows(text, DS)
    assert exc.value.text == errors.BAD_ARG.format(name="rows")


def test_extra_key_is_bad_arg_rows():
    with pytest.raises(KhError) as exc:
        parse(row() | {"extra": 1})
    assert exc.value.text == errors.BAD_ARG.format(name="rows")


def test_501_rows_are_too_many():
    with pytest.raises(KhError) as exc:
        parse(*[row(key=str(i)) for i in range(MAX_ROWS + 1)])
    assert exc.value.text == errors.TOO_MANY_ROWS


def test_row_problems():
    assert problem(row(key="")) == "0: key is invalid"
    assert problem(row(key="x" * 201)) == "0: key is invalid"
    assert problem(row(key=5)) == "0: key is invalid"
    assert problem(row(when="2026-11-01T10:00:00")) == "0: source_updated_at is invalid"
    assert problem(row(when="soon")) == "0: source_updated_at is invalid"
    assert problem(row() | {"fields": []}) == "0: fields is not an object"
    assert problem(row(nope=1)) == "0: unknown field nope"
    assert problem(row(**{"n" * 100: 1})) == f"0: unknown field {'n' * 64}"
    assert problem(row(amount="abc")) == "0: amount is not a valid number"
    assert problem(row(amount=True)) == "0: amount is not a valid number"
    assert problem(row(close_date="yesterday")) == "0: close_date is not a valid date"
    assert problem(row(won="yes")) == "0: won is not a valid bool"
    assert problem(row(name=5)) == "0: name is not a valid text"
    assert problem(row(stage="lost")) == "0: stage is not one of the choices"
    assert problem(row(key="a"), row(key="a")) == "1: duplicate key"
    assert problem(row(key="a", name="x"), row(key="b", stage="open")) == "1: fields differ from row 0"


def test_values_are_coerced():
    (a, b) = parse(
        row(key="a", amount="12.50", close_date="2026-11-01T10:00:00Z", won="1", stage="open"),
        row(key="b", amount=12, close_date="2026-11-02", won=False, stage=None),
    )
    assert a.values["amount"] == Decimal("12.50")
    assert a.values["close_date"] == date(2026, 11, 1)
    assert a.values["won"] is True
    assert b.values["amount"] == Decimal("12")
    assert b.values["won"] is False
    assert b.values["stage"] is None
    assert a.source_updated_at.utcoffset().total_seconds() == 0


def test_field_by_source_name_maps_to_the_column():
    (a,) = parse(row(**{"Amount__c": 7}))
    assert a.values == {"amount": Decimal("7")}


def test_null_is_none_for_every_type():
    assert all(coerce(c, None) is None for c in DS.columns)
