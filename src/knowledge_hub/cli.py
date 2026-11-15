"""The ``kh`` command line."""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from knowledge_hub.settings import load_settings

COMMANDS = ("serve", "migrate", "rebuild", "check", "daily", "recompute-visibility", "requeue-embeddings")


def _not_implemented() -> int:
    print("not implemented yet")
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kh")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        sub.add_parser(name)
    args = parser.parse_args(argv)
    if args.command == "serve":
        try:
            load_settings()
        except (ValidationError, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 2
    return _not_implemented()


if __name__ == "__main__":
    raise SystemExit(main())
