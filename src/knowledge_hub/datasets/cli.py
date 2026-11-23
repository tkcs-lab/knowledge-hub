"""``kh dataset``: define datasets and their columns, as an administrator (plan 72, 3.2)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path

import psycopg

from knowledge_hub.config import KhConfig, load_config
from knowledge_hub.datasets import ddl
from knowledge_hub.datasets.model import Column, Dataset, DatasetError
from knowledge_hub.settings import load_settings

log = logging.getLogger(__name__)


class _Refused(Exception):
    def __init__(self, text: str, code: int = 1) -> None:
        super().__init__(text)
        self.code = code


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get("KH_CONFIG_FILE")
    parser = argparse.ArgumentParser(prog="kh dataset")
    sub = parser.add_subparsers(dest="command", required=True)

    def mutating(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--by", required=True, help="email of the administrator")
        p.add_argument("--config", type=Path, default=Path(env) if env else None, help="path of kh.yaml")
        return p

    p = mutating("create", "create a dataset")
    p.add_argument("name")
    p.add_argument("--title", required=True)
    p.add_argument("--audiences", required=True, type=_csv)
    p.add_argument("--description", default="")

    p = mutating("add-column", "add a column")
    p.add_argument("dataset")
    p.add_argument("column")
    p.add_argument("--type", required=True, dest="col_type")
    p.add_argument("--label")
    p.add_argument("--choices", type=_csv, default=[])
    p.add_argument("--ref", default="")
    p.add_argument("--source-name", default="")
    for flag in ("filterable", "embed", "sensitive", "indexed"):
        p.add_argument(f"--{flag}", action="store_true")

    p = mutating("rename-column", "rename a column")
    p.add_argument("dataset")
    p.add_argument("old")
    p.add_argument("new")

    p = mutating("drop-column", "drop a column and its values")
    p.add_argument("dataset")
    p.add_argument("column")
    p.add_argument("--yes", action="store_true")

    p = mutating("set-flags", "change a column's flags, label or source name")
    p.add_argument("dataset")
    p.add_argument("column")
    for flag in ("filterable", "embed", "sensitive", "indexed"):
        p.add_argument(f"--{flag}", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--label")
    p.add_argument("--source-name")

    p = mutating("set-choices", "replace the choices of a choice column")
    p.add_argument("dataset")
    p.add_argument("column")
    p.add_argument("choices", type=_csv)

    p = mutating("set", "change a dataset")
    p.add_argument("dataset")
    p.add_argument("--title")
    p.add_argument("--description")
    p.add_argument("--title-column")
    p.add_argument("--audiences", type=_csv)
    p.add_argument("--max-outdated-percent", type=int)
    p.add_argument("--history", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--yes", action="store_true")

    for name, help_ in (("list", "list datasets"), ("show", "show a dataset")):
        p = sub.add_parser(name, help=help_)
        if name == "show":
            p.add_argument("dataset")
        p.add_argument("--json", action="store_true")
    return parser


def _flags(col: Column) -> str:
    return ",".join(f for f in ("filterable", "embed", "sensitive", "indexed") if getattr(col, f))


def _print_dataset(ds: Dataset, *, columns: bool) -> None:
    print(f"{ds.name}\t{ds.title}\t{','.join(ds.audiences)}\t{len(ds.columns)} columns")
    if columns:
        for c in ds.columns:
            print(f"{c.name}\t{c.type}\t{_flags(c)}")


def _check_audiences(config: KhConfig, audiences: list[str]) -> None:
    for audience in audiences:
        if not config.is_active(audience):
            raise _Refused(f"audience {audience} is not an active group")


def _mutate(args: argparse.Namespace, config: KhConfig, dsn: str) -> str:
    cmd = args.command
    if cmd == "create":
        _check_audiences(config, args.audiences)
        ddl.create_dataset(dsn, args.name, args.title, args.audiences, args.description)
        return f"created {args.name}"
    if cmd == "add-column":
        col = Column(
            name=args.column,
            type=args.col_type,
            label=args.label or args.column,
            ordinal=0,
            choices=tuple(args.choices),
            ref_dataset=args.ref,
            source_name=args.source_name,
            filterable=args.filterable,
            embed=args.embed,
            sensitive=args.sensitive,
            indexed=args.indexed,
        )
        ddl.add_column(dsn, args.dataset, col)
        return f"added column {args.column} to {args.dataset}"
    if cmd == "rename-column":
        ddl.rename_column(dsn, args.dataset, args.old, args.new)
        return f"renamed column {args.old} to {args.new} in {args.dataset}"
    if cmd == "drop-column":
        if not args.yes:
            raise _Refused(f"add --yes to drop column {args.column} and its values", 2)
        ddl.drop_column(dsn, args.dataset, args.column)
        return f"dropped column {args.column} from {args.dataset}"
    if cmd == "set-flags":
        ddl.set_flags(
            dsn,
            args.dataset,
            args.column,
            filterable=args.filterable,
            embed=args.embed,
            sensitive=args.sensitive,
            indexed=args.indexed,
            label=args.label,
            source_name=args.source_name,
        )
        return f"updated column {args.column} of {args.dataset}"
    if cmd == "set-choices":
        ddl.set_choices(dsn, args.dataset, args.column, args.choices)
        return f"updated choices of {args.column} in {args.dataset}"
    if args.audiences is not None:
        _check_audiences(config, args.audiences)
    if args.history is False and not args.yes and ddl.get_dataset(dsn, args.dataset).history:
        raise _Refused(f"add --yes to drop the history of {args.dataset}", 2)
    ddl.set_dataset(
        dsn,
        args.dataset,
        title=args.title,
        description=args.description,
        title_column=args.title_column,
        audiences=args.audiences,
        max_outdated_percent=args.max_outdated_percent,
        history=args.history,
    )
    return f"updated {args.dataset}"


def _dataset_of(args: argparse.Namespace) -> str:
    return getattr(args, "name", None) or getattr(args, "dataset", "") or ""


def _read(args: argparse.Namespace, dsn: str) -> int:
    if args.command == "list":
        items = ddl.list_catalog(dsn)
        if args.json:
            print(json.dumps([dataclasses.asdict(d) for d in items], sort_keys=True))
        else:
            for ds in items:
                _print_dataset(ds, columns=False)
        return 0
    ds = ddl.get_dataset(dsn, args.dataset)
    if args.json:
        print(json.dumps(dataclasses.asdict(ds), sort_keys=True))
    else:
        _print_dataset(ds, columns=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command in ("list", "show"):
            return _read(args, load_settings().dsn("migrator"))
        email = args.by.lower()
        if args.config is None:
            print("dataset: --config or KH_CONFIG_FILE is required")
            return 2
        config = load_config(args.config)
        if email not in config.admins:
            print(f"dataset: {email} is not an administrator")
            return 1
        done = _mutate(args, config, load_settings().dsn("migrator"))
    except _Refused as exc:
        print(f"dataset: {exc}")
        return exc.code
    except DatasetError as exc:
        print(f"dataset: {exc}")
        return 1
    except psycopg.Error as exc:
        print(f"dataset: database error: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
        return 1
    except ValueError as exc:  # config errors
        print(f"dataset: {exc}")
        return 2
    log.info("dataset %s by=%s dataset=%s", args.command, email, _dataset_of(args))
    print(f"dataset: {done}")
    return 0

