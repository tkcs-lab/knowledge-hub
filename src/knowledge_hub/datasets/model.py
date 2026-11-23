"""The dataset model: names, column rules and value conversion. Pure, no I/O (plan 3.2)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

DATASET_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")  # plus: no "__", not ending "_" (check_dataset_name)
COLUMN_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SAVED_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SYNC_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
COLUMN_TYPES = ("text", "number", "date", "bool", "choice", "ref")
PG_TYPES = {
    "text": "text",
    "number": "numeric",
    "date": "date",
    "bool": "boolean",
    "choice": "text",
    "ref": "text",
}
RESERVED_COLUMNS = frozenset({"oid", "tableoid", "xmin", "xmax", "cmin", "cmax", "ctid"})
MAX_COLUMNS = 200
MAX_CHOICES = 100
MAX_TEXT = 200


class DatasetError(ValueError):
    """A refusal with a message fit for the operator."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    label: str
    ordinal: int
    choices: tuple[str, ...] = ()
    ref_dataset: str = ""
    source_name: str = ""
    filterable: bool = False
    embed: bool = False
    sensitive: bool = False
    indexed: bool = False


@dataclass(frozen=True)
class Dataset:
    name: str
    title: str
    description: str
    audiences: tuple[str, ...]
    title_column: str
    history: bool
    max_outdated_percent: int
    columns: tuple[Column, ...]

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def field(self, name: str) -> Column | None:
        """Find a column by its name first, then by its ``source_name``."""
        found = self.column(name)
        if found is not None:
            return found
        return next((c for c in self.columns if c.source_name and c.source_name == name), None)


def object_name(kind: str, *parts: str) -> str:
    """A short stable name for an index or constraint inside the shared ``kh_data`` namespace."""
    digest = hashlib.sha256(".".join(parts).encode()).hexdigest()[:8]
    return f"{kind}_{digest}"


def check_dataset_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not DATASET_RE.fullmatch(name)
        or "__" in name
        or name.endswith("_")
    ):
        raise DatasetError(f"dataset name {name} is not valid")
    return name


def check_text(value: str, what: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TEXT
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise DatasetError(f"{what} is not valid")
    return value


def check_new_column(ds: Dataset, col: Column) -> None:
    """Refuse a column that cannot be added to ``ds``."""
    if not COLUMN_RE.fullmatch(col.name) or col.name in RESERVED_COLUMNS:
        raise DatasetError(f"column name {col.name} is not valid")
    if col.type not in COLUMN_TYPES:
        raise DatasetError(f"column type {col.type} is not valid")
    if ds.column(col.name) is not None:
        raise DatasetError(f"column {col.name} exists")
    if len(ds.columns) >= MAX_COLUMNS:
        raise DatasetError(f"a dataset has at most {MAX_COLUMNS} columns")
    if col.embed and col.sensitive:
        raise DatasetError("embed and sensitive cannot both be set")
    check_text(col.label, "label")
    choices_ok = (
        (col.type == "choice") == bool(col.choices)
        and len(col.choices) <= MAX_CHOICES
        and all(_choice_ok(c) for c in col.choices)
    )
    if not choices_ok:
        raise DatasetError(
            f"choices are for choice columns only, and a choice column needs 1 to {MAX_CHOICES} of them"
        )
    if (col.type == "ref") != bool(col.ref_dataset):
        raise DatasetError("ref is for ref columns only, and a ref column needs it")
    if col.source_name:
        taken = {c.name for c in ds.columns} | {c.source_name for c in ds.columns if c.source_name}
        taken.add(col.name)
        if col.source_name in taken:
            raise DatasetError(f"source_name {col.source_name} is already used")
        check_text(col.source_name, "source_name")


def _choice_ok(value: str) -> bool:
    try:
        check_text(value, "choice")
    except DatasetError:
        return False
    return True


def to_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value
