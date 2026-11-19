"""The panel's operation allowlist: validation of the arguments and the ``kh`` argv (plan 63, 3.6).

Pure: no I/O and no subprocess. Every value reaches the command line after a ``--``
(or as the value of a ``--option=value`` token), so no value is ever read as an option.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from knowledge_hub.config import DIR_RE, GROUP_RE, LABEL_RE, LOGIN_RE, REPO_RE

MAX_LIST = 50
MAX_IDS = 100
MAX_DESCRIPTION = 200
MAX_EMAIL = 254
CFG = "\0config"  # placeholder where ``argv`` inserts the common options
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class OpError(ValueError):
    """A refused operation; ``reason`` is the text shown to the caller (a 400 ``bad_args``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Op:
    name: str
    kind: str  # "cli" or "inproc"
    keys: tuple[str, ...]
    optional: tuple[str, ...]
    build: Callable[[dict[str, Any]], list[str]]

    @property
    def is_config_op(self) -> bool:
        return self.kind == "cli" and not self.name.startswith("health.")


@dataclass(frozen=True)
class ValidatedOp:
    op: Op
    args: dict[str, Any]
    dry_run: bool = False
    config_sha256: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.op.name

    @property
    def kind(self) -> str:
        return self.op.kind


# ---- value rules: each returns the value or raises OpError ----


def _pattern(label: str, rx: re.Pattern[str]) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if not isinstance(value, str) or not rx.fullmatch(value):
            raise OpError(f"{label} is not valid")
        return value

    return check


def _group_name(value: Any) -> str:
    name = _pattern("group name", GROUP_RE)(value)
    if name == "owners" or name.endswith("-approve"):
        raise OpError("group name is not valid")
    return name


def _description(value: Any) -> str:
    if not isinstance(value, str):
        raise OpError("description must be a string")
    if len(value) > MAX_DESCRIPTION:
        raise OpError(f"description is longer than {MAX_DESCRIPTION} characters")
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise OpError("description has a control character")
    return value


def _email(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_EMAIL or value.count("@") != 1:
        raise OpError("email is not valid")
    local, domain = value.split("@")
    if not local or not domain:
        raise OpError("email is not valid")
    if any(c.isspace() or unicodedata.category(c) == "Cc" for c in value):
        raise OpError("email is not valid")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise OpError("value must be true or false")
    return value


def _list_of(item: Callable[[Any], Any], label: str) -> Callable[[Any], list[Any]]:
    def check(value: Any) -> list[Any]:
        if not isinstance(value, list):
            raise OpError(f"{label} must be a list")
        if len(value) > MAX_LIST:
            raise OpError(f"{label} has more than {MAX_LIST} items")
        return [item(v) for v in value]

    return check


def _ids(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        raise OpError("ids must be a non-empty list")
    if len(value) > MAX_IDS:
        raise OpError(f"ids has more than {MAX_IDS} items")
    for v in value:
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise OpError("ids must be positive integers")
    return list(value)


_label = _pattern("label name", LABEL_RE)
_login = _pattern("login", LOGIN_RE)
_repo = _pattern("repo name", REPO_RE)
_dirs = _list_of(_pattern("directory", DIR_RE), "dirs")
_labels = _list_of(_pattern("label name", LABEL_RE), "labels")
_logins = _list_of(_login, "logins")

RULES: dict[str, Callable[[Any], Any]] = {
    "description": _description,
    "login": _login,
    "email": _email,
    "from_groups": _bool,
    "members": _logins,
    "approvers": _logins,
    "confirm": _bool,
    "group": _group_name,
    "with_approver": _bool,
    "dirs": _dirs,
    "labels": _labels,
    "repo": _repo,
    "ids": _ids,
}

# ``name`` means a label, a group or a repo depending on the op.
_NAME_RULES: dict[str, Callable[[Any], str]] = {"label": _label, "group": _group_name, "repo": _repo}


# ---- argv builders (the part after ``kh``; ``cfg`` options are added by ``argv``) ----


def _csv(values: list[str]) -> str:
    return ",".join(values)


def _cmd(
    words: list[str],
    options: Callable[[dict[str, Any]], list[str]],
    positionals: Callable[[dict[str, Any]], list[str]],
):
    def build(args: dict[str, Any]) -> list[str]:
        return [*words, CFG, *options(args), "--", *positionals(args)]

    return build


def _no_options(args: dict[str, Any]) -> list[str]:
    return []


def _name_only(words: list[str]) -> Callable[[dict[str, Any]], list[str]]:
    return _cmd(words, _no_options, lambda a: [a["name"]])


def _flag(args: dict[str, Any], key: str, option: str) -> list[str]:
    return [option] if args.get(key) else []


def _repo_options(args: dict[str, Any]) -> list[str]:
    out = []
    if args.get("dirs") is not None:
        out.append(f"--dirs={_csv(args['dirs'])}")
    if args.get("labels") is not None:
        out.append(f"--labels={_csv(args['labels'])}")
    return out


def _inproc(_args: dict[str, Any]) -> list[str]:
    raise OpError("this operation runs in the panel process and has no command line")


def _plain(words: list[str]) -> Callable[[dict[str, Any]], list[str]]:
    return lambda args: list(words)


def _setup_plan(args: dict[str, Any]) -> list[str]:
    return ["setup", "plan", CFG]


def _cli(
    name: str,
    keys: tuple[str, ...],
    build: Callable[[dict[str, Any]], list[str]],
    optional: tuple[str, ...] = (),
) -> Op:
    return Op(name, "cli", keys, optional, build)


def _in(name: str, optional: tuple[str, ...] = ()) -> Op:
    return Op(name, "inproc", (), optional, _inproc)


def _build_ops() -> dict[str, Op]:
    ops: list[Op] = [
        _cli(
            "label.add",
            ("name", "description"),
            _cmd(
                ["label", "add"],
                lambda a: [f"--description={a['description']}"],
                lambda a: [a["name"]],
            ),
        ),
        _cli(
            "label.describe",
            ("name", "description"),
            _cmd(["label", "rename"], _no_options, lambda a: [a["name"], a["description"]]),
        ),
        _cli("label.archive", ("name",), _name_only(["label", "archive"])),
        _cli("label.unarchive", ("name",), _name_only(["label", "unarchive"])),
        _cli("label.remove", ("name",), _name_only(["label", "remove"])),
        _cli(
            "person.add",
            ("login", "email"),
            _cmd(["person", "add"], _no_options, lambda a: [a["login"], a["email"]]),
        ),
        _cli(
            "person.remove",
            ("login", "from_groups"),
            _cmd(
                ["person", "remove"],
                lambda a: _flag(a, "from_groups", "--from-groups"),
                lambda a: [a["login"]],
            ),
        ),
        _cli(
            "group.add",
            ("name", "members", "approvers"),
            _cmd(
                ["group", "add"],
                lambda a: [f"--members={_csv(a['members'])}", f"--approvers={_csv(a['approvers'])}"],
                lambda a: [a["name"]],
            ),
        ),
        _cli("group.archive", ("name",), _name_only(["group", "archive"])),
        _cli("group.unarchive", ("name",), _name_only(["group", "unarchive"])),
        _cli(
            "group.remove",
            ("name", "confirm"),
            _cmd(["group", "remove"], lambda a: _flag(a, "confirm", "--yes"), lambda a: [a["name"]]),
        ),
        _cli(
            "member.add",
            ("group", "login"),
            _cmd(["group", "member", "add"], _no_options, lambda a: [a["group"], a["login"]]),
        ),
        _cli(
            "member.remove",
            ("group", "login", "with_approver"),
            _cmd(
                ["group", "member", "remove"],
                lambda a: _flag(a, "with_approver", "--with-approver"),
                lambda a: [a["group"], a["login"]],
            ),
        ),
        _cli(
            "approver.add",
            ("group", "login"),
            _cmd(["group", "approver", "add"], _no_options, lambda a: [a["group"], a["login"]]),
        ),
        _cli(
            "approver.remove",
            ("group", "login"),
            _cmd(["group", "approver", "remove"], _no_options, lambda a: [a["group"], a["login"]]),
        ),
        _cli(
            "repo.add",
            ("name",),
            _cmd(["repo", "add"], _repo_options, lambda a: [a["name"]]),
            optional=("dirs", "labels"),
        ),
        _cli(
            "repo.set",
            ("name",),
            _cmd(["repo", "set"], _repo_options, lambda a: [a["name"]]),
            optional=("dirs", "labels"),
        ),
        _cli(
            "repo.remove",
            ("name", "confirm"),
            _cmd(["repo", "remove"], lambda a: _flag(a, "confirm", "--yes"), lambda a: [a["name"]]),
        ),
        _cli("health.setup_plan", (), _setup_plan),
        _cli("health.index_check", (), _plain(["check"])),
        Op("repo.rebuild", "inproc", ("repo",), (), _inproc),
        _in("jobs.retry", optional=("ids",)),
        _in("jobs.reclaim"),
        _in("embeddings.requeue"),
    ]
    return {o.name: o for o in ops}


OPS: dict[str, Op] = _build_ops()

# which kind of name each op takes
_NAME_KIND = {
    "label.add": "label",
    "label.describe": "label",
    "label.archive": "label",
    "label.unarchive": "label",
    "label.remove": "label",
    "group.add": "group",
    "group.archive": "group",
    "group.unarchive": "group",
    "group.remove": "group",
    "repo.add": "repo",
    "repo.set": "repo",
    "repo.remove": "repo",
}


def validate(op: str, args: object, dry_run: object, config_sha256: object) -> ValidatedOp:
    spec = OPS.get(op) if isinstance(op, str) else None
    if spec is None:
        raise OpError("unknown operation")
    if not isinstance(args, dict) or not all(isinstance(k, str) for k in args):
        raise OpError("args must be an object")
    allowed = {*spec.keys, *spec.optional}
    extra = sorted(set(args) - allowed)
    if extra:
        raise OpError(f"unexpected argument: {extra[0][:40]}")
    missing = [k for k in spec.keys if k not in args]
    if missing:
        raise OpError(f"missing argument: {missing[0]}")
    clean: dict[str, Any] = {}
    for key, value in args.items():
        rule = _NAME_RULES[_NAME_KIND[op]] if key == "name" else RULES[key]
        clean[key] = rule(value)
    if op == "repo.set" and not ({"dirs", "labels"} & clean.keys()):
        raise OpError("repo.set needs dirs or labels")
    if not isinstance(dry_run, bool):
        raise OpError("dry_run must be true or false")
    if dry_run and not spec.is_config_op:
        raise OpError("dry_run is not available for this operation")
    sha: str | None = None
    if config_sha256 is not None:
        if not spec.is_config_op:
            raise OpError("config_sha256 is not available for this operation")
        if not isinstance(config_sha256, str) or not SHA256_RE.fullmatch(config_sha256):
            raise OpError("config_sha256 must be 64 lower case hex digits")
        sha = config_sha256
    return ValidatedOp(spec, clean, dry_run, sha)


def argv(valid: ValidatedOp, config_file: str) -> list[str]:
    """The ``kh`` arguments (without the interpreter) for a validated ``cli`` operation."""
    built = valid.op.build(valid.args)
    common: list[str] = [f"--config={config_file}"]
    if valid.op.is_config_op:
        if valid.dry_run:
            common.append("--dry-run")
        if valid.config_sha256 is not None:
            common.append(f"--if-sha256={valid.config_sha256}")
    if CFG not in built:
        return built
    i = built.index(CFG)
    if valid.name == "health.setup_plan":
        return [*built[:i], *common]
    return [*built[:i], *common, *built[i + 1 :]]
