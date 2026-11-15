"""Registry of tool cases for the leak matrix (plan 3.3).

Every tool that returns or searches rows appends a ``ToolCase`` here; ``test_leak_matrix`` then runs
it for every principal and every seed visibility case. A case names, for one seed case, the call
that targets that row, the call that targets a row that does not exist, and how to read the
response.

``ids`` is the seed result ``{table: {case: id}}`` of ``tests.support.seed.seed_rows``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tests.support.mcp_client import ToolResult
from tests.support.seed import CASES

Ids = dict[str, dict[str, str]]
ArgsFor = Callable[[Ids, str], dict[str, Any]]


@dataclass(frozen=True)
class ToolCase:
    name: str  # the tool
    args_for_hidden: ArgsFor  # (ids, case): arguments aimed at that row, used when it is hidden
    args_for_visible: ArgsFor  # (ids, case): arguments aimed at that row, used when it is visible
    expect_hidden: Callable[[ToolResult], None]  # asserts the response of a call that reaches nothing
    args_for_missing: Callable[[Ids], dict[str, Any]]  # arguments aimed at a row that does not exist
    cited: Callable[[ToolResult], set[str]]  # citation ids a response contains
    citation: Callable[[Ids, str], str]  # citation id of the seed row of a case
    cases: tuple[str, ...] = tuple(CASES)  # seed cases the call can single out
    label: str = ""

    @property
    def id(self) -> str:
        return f"{self.name}-{self.label}" if self.label else self.name


def _not_found(res: ToolResult) -> None:
    assert res.status == 200 and res.is_error and res.text.endswith("not found"), res


def _get_doc_case(table: str, prefix: str) -> ToolCase:
    def args(ids: Ids, case: str) -> dict[str, Any]:
        return {"id": f"{prefix}:{ids[table][case]}"}

    return ToolCase(
        name="get_doc",
        args_for_hidden=args,
        args_for_visible=args,
        expect_hidden=_not_found,
        args_for_missing=lambda ids: {"id": f"{prefix}:{uuid.uuid4()}"},
        cited=lambda res: {res.data["citation_id"]} if res.data else set(),
        citation=lambda ids, case: f"{prefix}:{ids[table][case]}",
        label=prefix,
    )


# The seed gives every chunk the text "chunk <case>"; websearch AND-s the words. "all" is a
# stop word, so the "all" case cannot be singled out by text and is left to get_doc.
_SEARCH_CASES = tuple(c for c in CASES if c != "all")


def _search_args(ids: Ids, case: str) -> dict[str, Any]:
    return {"query": f"chunk {case}", "min_tier": "T3", "limit": 50}


def _search_empty(res: ToolResult) -> None:
    assert res.status == 200 and not res.is_error, res
    assert res.data["hits"] == [] and res.data["next_cursor"] == "", res


SEARCH = ToolCase(
    name="search",
    args_for_hidden=_search_args,
    args_for_visible=_search_args,
    expect_hidden=_search_empty,
    args_for_missing=lambda ids: {"query": "chunk zzqxjv", "min_tier": "T3", "limit": 50},
    cited=lambda res: {h["citation_id"] for h in (res.data or {}).get("hits", [])},
    citation=lambda ids, case: f"doc:{ids['documents'][case]}",
    cases=_SEARCH_CASES,
)

def _ingest_args(ids: Ids, case: str = "") -> dict[str, Any]:
    return {
        "source": "meet",
        "source_ref": f"matrix-{uuid.uuid4()}",
        "title": "Matrix",
        "body": "matrix body",
        "domain": "technical",
        "occurred_at": "2026-01-02T03:04:05+00:00",
        "participants": ["nobody@example.org"],
    }


def _ingest_not_a_leak(res: ToolResult) -> None:
    assert res.status == 200, res


# ingest_raw reads no stored row, so no seed case singles one out (``cases=()``); the matrix still
# calls it once per principal with a fresh ingest and checks that it cites no hidden seed row.
INGEST_RAW = ToolCase(
    name="ingest_raw",
    args_for_hidden=_ingest_args,
    args_for_visible=_ingest_args,
    expect_hidden=_ingest_not_a_leak,
    args_for_missing=_ingest_args,
    cited=lambda res: {res.data["citation_id"]} if res.data else set(),
    citation=lambda ids, case: "",
    cases=(),
)

def _derive_args(ids: Ids, case: str = "") -> dict[str, Any]:
    return {
        "kind": "summary",
        "title": "Matrix",
        "body": "matrix summary",
        "domain": "technical",
        "claims_json": "[]",
        "derived_from": [f"doc:{ids['documents'][case]}" if case else f"doc:{uuid.uuid4()}"],
        "pipeline": "p",
        "model": "m",
        "pipeline_version": "1",
    }


def _derive_missing(ids: Ids) -> dict[str, Any]:
    return _derive_args(ids)


# write_derived creates a new row, so no seed row is cited back (``cases=()``); the matrix calls it
# once per principal with a source that does not exist and checks the response cites no hidden row.
# The hidden-versus-missing comparison for each seed case is in ``test_write_derived``.
WRITE_DERIVED = ToolCase(
    name="write_derived",
    args_for_hidden=_derive_args,
    args_for_visible=_derive_args,
    expect_hidden=_ingest_not_a_leak,
    args_for_missing=_derive_missing,
    cited=lambda res: {res.data["citation_id"]} if res.data else set(),
    citation=lambda ids, case: "",
    cases=(),
)

TOOL_CASES: list[ToolCase] = [
    SEARCH,
    _get_doc_case("documents", "doc"),
    _get_doc_case("raw_records", "raw"),
    _get_doc_case("derived_records", "derived"),
    INGEST_RAW,
    WRITE_DERIVED,
]


def _propose_args(ids: Ids, case: str = "") -> dict[str, Any]:
    return {
        "domain": "technical",
        "title": "Matrix",
        "content": f"matrix {uuid.uuid4()}",
        "rationale": "matrix",
        "source_ids": [f"doc:{ids['documents'][case]}" if case else f"doc:{uuid.uuid4()}"],
        "new_path": f"technical/matrix-{uuid.uuid4().hex[:8]}.md",
    }


# propose_change creates a new row and cites none back (``cases=()``); the matrix calls it once per
# principal with a source that does not exist. The per-case comparison is in ``test_propose_change``.
PROPOSE_CHANGE = ToolCase(
    name="propose_change",
    args_for_hidden=_propose_args,
    args_for_visible=_propose_args,
    expect_hidden=_ingest_not_a_leak,
    args_for_missing=lambda ids: _propose_args(ids),
    cited=lambda res: set(),
    citation=lambda ids, case: "",
    cases=(),
)
TOOL_CASES.append(PROPOSE_CHANGE)


def _flag_args(ids: Ids, case: str = "") -> dict[str, Any]:
    first = f"doc:{ids['documents'][case]}" if case else f"doc:{uuid.uuid4()}"
    return {"a": first, "b": f"doc:{uuid.uuid4()}", "note": "matrix"}


# flag_conflict creates a row and cites none back (``cases=()``); the matrix calls it once per principal
# with ids that do not exist. The per-case comparison is in ``test_conflicts``.
FLAG_CONFLICT = ToolCase(
    name="flag_conflict",
    args_for_hidden=_flag_args,
    args_for_visible=_flag_args,
    expect_hidden=_not_found,
    args_for_missing=lambda ids: _flag_args(ids),
    cited=lambda res: set(),
    citation=lambda ids, case: "",
    cases=(),
)


def _list_cited(key: str, field: str) -> Callable[[ToolResult], set[str]]:
    def cited(res: ToolResult) -> set[str]:
        out: set[str] = set()
        for entry in (res.data or {}).get(key, []):
            value = entry[field]
            out.update(value if isinstance(value, list) else [value])
        return out

    return cited


# The list tools have no filter that singles out one seed case (``cases=()``); the matrix calls each
# once per principal and checks that no hidden seed row is cited. Exact page contents are in the
# tool's own test module.
LIST_CONFLICTS = ToolCase(
    name="list_conflicts",
    args_for_hidden=lambda ids, case="": {"limit": 50},
    args_for_visible=lambda ids, case="": {"limit": 50},
    expect_hidden=_ingest_not_a_leak,
    args_for_missing=lambda ids: {"limit": 50},
    cited=_list_cited("conflicts", "items"),
    citation=lambda ids, case: "",
    cases=(),
)
LIST_STALE = ToolCase(
    name="list_stale",
    args_for_hidden=lambda ids, case="": {"limit": 50},
    args_for_visible=lambda ids, case="": {"limit": 50},
    expect_hidden=_ingest_not_a_leak,
    args_for_missing=lambda ids: {"limit": 50},
    cited=_list_cited("documents", "citation_id"),
    citation=lambda ids, case: "",
    cases=(),
)
TOOL_CASES.extend([FLAG_CONFLICT, LIST_CONFLICTS, LIST_STALE])
