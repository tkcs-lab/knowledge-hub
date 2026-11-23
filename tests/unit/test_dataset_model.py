"""The pure dataset model (plan 3.2)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from knowledge_hub.datasets.model import (
    Column,
    Dataset,
    DatasetError,
    check_dataset_name,
    check_new_column,
    check_text,
    object_name,
    to_json_value,
)

pytestmark = pytest.mark.unit


def _ds(*columns: Column) -> Dataset:
    return Dataset("deals", "Deals", "", ("all",), "", False, 20, tuple(columns))


def _col(name="amount", type="number", **kw) -> Column:
    return Column(name, type, kw.pop("label", "Label"), kw.pop("ordinal", 1), **kw)


def _refused(ds: Dataset, col: Column, text: str) -> None:
    with pytest.raises(DatasetError) as err:
        check_new_column(ds, col)
    assert str(err.value) == text


def test_invalid_and_reserved_names():
    _refused(_ds(), _col(name="Bad"), "column name Bad is not valid")
    _refused(_ds(), _col(name="_key"), "column name _key is not valid")
    _refused(_ds(), _col(name="xmin"), "column name xmin is not valid")


def test_name_in_use():
    _refused(_ds(_col()), _col(), "column amount exists")


def test_too_many_columns():
    cols = [_col(name=f"c{i}") for i in range(200)]
    _refused(_ds(*cols), _col(name="one_more"), "a dataset has at most 200 columns")


def test_embed_and_sensitive():
    _refused(_ds(), _col(embed=True, sensitive=True), "embed and sensitive cannot both be set")


CHOICES_TEXT = "choices are for choice columns only, and a choice column needs 1 to 100 of them"


@pytest.mark.parametrize(
    "col",
    [
        _col(choices=("a",)),
        _col(name="stage", type="choice"),
        _col(name="stage", type="choice", choices=tuple(f"c{i}" for i in range(101))),
        _col(name="stage", type="choice", choices=("a\nb",)),
        _col(name="stage", type="choice", choices=("",)),
    ],
)
def test_choices(col):
    _refused(_ds(), col, CHOICES_TEXT)


def test_ref():
    text = "ref is for ref columns only, and a ref column needs it"
    _refused(_ds(), _col(ref_dataset="accounts"), text)
    _refused(_ds(), _col(name="account", type="ref"), text)


def test_source_name_taken():
    ds = _ds(_col(source_name="Amount__c"))
    _refused(ds, _col(name="other", source_name="Amount__c"), "source_name Amount__c is already used")
    _refused(ds, _col(name="other", source_name="amount"), "source_name amount is already used")


def test_valid_column_passes():
    check_new_column(_ds(), _col(source_name="Amount__c"))


@pytest.mark.parametrize("name", ["a__b", "a_", "A", "1a", "", "a" * 41])
def test_dataset_name_refused(name):
    with pytest.raises(DatasetError, match="is not valid"):
        check_dataset_name(name)


def test_dataset_name_ok():
    assert check_dataset_name("sf_opportunities") == "sf_opportunities"


def test_check_text():
    assert check_text("ok", "label") == "ok"
    for bad in ("", "x" * 201, "a\x00b", "a\tb"):
        with pytest.raises(DatasetError) as err:
            check_text(bad, "label")
        assert str(err.value) == "label is not valid"


def test_object_name_is_short_and_stable():
    a = object_name("ix", "deals", "amount")
    assert len(a) == 11 and a.startswith("ix_")
    assert a == object_name("ix", "deals", "amount")
    assert a != object_name("ix", "deals", "stage")
    assert a != object_name("ck", "deals", "amount")


def test_field_by_name_then_source_name():
    ds = _ds(_col(source_name="Amount__c"), _col(name="stage", type="text"))
    assert ds.field("amount").name == "amount"
    assert ds.field("Amount__c").name == "amount"
    assert ds.field("stage").name == "stage"
    assert ds.field("nope") is None
    assert ds.column("Amount__c") is None


def test_to_json_value():
    assert to_json_value(Decimal("2")) == 2 and isinstance(to_json_value(Decimal("2")), int)
    assert to_json_value(Decimal("2.5")) == 2.5
    assert to_json_value(date(2026, 11, 23)) == "2026-11-23"
    assert to_json_value("x") == "x"
