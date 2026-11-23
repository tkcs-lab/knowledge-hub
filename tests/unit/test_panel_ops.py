import argparse
import importlib

import pytest

from knowledge_hub.admin.cli import COMMAND_MODULES
from knowledge_hub.panel import ops
from knowledge_hub.panel.ops import OPS, OpError, argv, validate

pytestmark = pytest.mark.unit

CFG = "--config=/etc/kh.yaml"
SHA = "a" * 64
IFSHA = f"--if-sha256={SHA}"

MOVE_ID = "rec:0a1b2c3d-0000-4000-8000-0123456789ab"

# (op, args, dry_run, sha, expected argv)
EXAMPLES = [
    (
        "label.add",
        {"name": "ops", "description": "Operations"},
        False,
        None,
        ["label", "add", CFG, "--description=Operations", "--", "ops"],
    ),
    (
        "label.describe",
        {"name": "ops", "description": "New text"},
        True,
        SHA,
        ["label", "rename", CFG, "--dry-run", IFSHA, "--", "ops", "New text"],
    ),
    ("label.archive", {"name": "ops"}, False, None, ["label", "archive", CFG, "--", "ops"]),
    ("label.unarchive", {"name": "ops"}, False, SHA, ["label", "unarchive", CFG, IFSHA, "--", "ops"]),
    ("label.remove", {"name": "ops"}, True, None, ["label", "remove", CFG, "--dry-run", "--", "ops"]),
    (
        "person.add",
        {"login": "alice", "email": "Alice@Example.com"},
        False,
        None,
        ["person", "add", CFG, "--", "alice", "Alice@Example.com"],
    ),
    (
        "person.remove",
        {"login": "alice", "from_groups": True},
        False,
        None,
        ["person", "remove", CFG, "--from-groups", "--", "alice"],
    ),
    (
        "person.remove",
        {"login": "alice", "from_groups": False},
        False,
        None,
        ["person", "remove", CFG, "--", "alice"],
    ),
    (
        "group.add",
        {"name": "eng", "members": ["a", "b"], "approvers": ["a"]},
        False,
        None,
        ["group", "add", CFG, "--members=a,b", "--approvers=a", "--", "eng"],
    ),
    ("group.archive", {"name": "eng"}, False, None, ["group", "archive", CFG, "--", "eng"]),
    ("group.unarchive", {"name": "eng"}, False, None, ["group", "unarchive", CFG, "--", "eng"]),
    (
        "group.remove",
        {"name": "eng", "confirm": True},
        False,
        None,
        ["group", "remove", CFG, "--yes", "--", "eng"],
    ),
    (
        "group.remove",
        {"name": "eng", "confirm": False},
        True,
        None,
        ["group", "remove", CFG, "--dry-run", "--", "eng"],
    ),
    (
        "member.add",
        {"group": "eng", "login": "bob"},
        False,
        None,
        ["group", "member", "add", CFG, "--", "eng", "bob"],
    ),
    (
        "member.remove",
        {"group": "eng", "login": "bob", "with_approver": True},
        False,
        None,
        ["group", "member", "remove", CFG, "--with-approver", "--", "eng", "bob"],
    ),
    (
        "approver.add",
        {"group": "eng", "login": "bob"},
        False,
        None,
        ["group", "approver", "add", CFG, "--", "eng", "bob"],
    ),
    (
        "approver.remove",
        {"group": "eng", "login": "bob"},
        False,
        None,
        ["group", "approver", "remove", CFG, "--", "eng", "bob"],
    ),
    ("repo.add", {"name": "tech-a"}, False, None, ["repo", "add", CFG, "--", "tech-a"]),
    (
        "repo.add",
        {"name": "tech-a", "dirs": ["docs", "a/b"], "labels": ["x"]},
        False,
        None,
        ["repo", "add", CFG, "--dirs=docs,a/b", "--labels=x", "--", "tech-a"],
    ),
    (
        "repo.set",
        {"name": "tech-a", "labels": []},
        False,
        None,
        ["repo", "set", CFG, "--labels=", "--", "tech-a"],
    ),
    (
        "repo.set",
        {"name": "tech-a", "dirs": ["docs"]},
        True,
        SHA,
        ["repo", "set", CFG, "--dry-run", IFSHA, "--dirs=docs", "--", "tech-a"],
    ),
    (
        "repo.remove",
        {"name": "tech-a", "confirm": True},
        False,
        None,
        ["repo", "remove", CFG, "--yes", "--", "tech-a"],
    ),
    ("health.setup_plan", {}, False, None, ["setup", "plan", CFG]),
    ("health.index_check", {}, False, None, ["check"]),
    (
        "item.move",
        {"id": MOVE_ID, "from_group": "mgmt", "to_group": "all"},
        False,
        None,
        ["move", CFG, "--from=mgmt", "--to=all", "--by=a@b.test", "--", MOVE_ID],
    ),
]

INPROC = [
    ("repo.rebuild", {"repo": "kb-eng"}),
    ("jobs.retry", {}),
    ("jobs.retry", {"ids": [1, 2]}),
    ("jobs.reclaim", {}),
    ("embeddings.requeue", {}),
]


BY_ = "--by=a@b.test"
FULL_COLUMN = {
    "dataset": "deals", "column": "stage", "type": "choice", "label": "Stage", "choices": ["a", "b"],
    "ref": "accounts", "source_name": "Stage__c", "filterable": True, "embed": True,
    "sensitive": True, "indexed": True,
}
DATASET_EXAMPLES = [
    (
        "dataset.create",
        {"dataset": "deals", "title": "Deals", "audiences": ["all", "mgmt"]},
        ["dataset", "create", CFG, "--title=Deals", "--audiences=all,mgmt", BY_, "--", "deals"],
    ),
    (
        "dataset.create",
        {"dataset": "deals", "title": "-x", "audiences": ["all"], "description": "d"},
        ["dataset", "create", CFG, "--title=-x", "--audiences=all", "--description=d", BY_, "--", "deals"],
    ),
    (
        "dataset.add_column",
        {"dataset": "deals", "column": "amount", "type": "number"},
        ["dataset", "add-column", CFG, "--type=number", BY_, "--", "deals", "amount"],
    ),
    (
        "dataset.add_column",
        FULL_COLUMN,
        [
            "dataset", "add-column", CFG, "--type=choice", "--label=Stage", "--choices=a,b",
            "--ref=accounts", "--source-name=Stage__c", "--filterable", "--embed", "--sensitive",
            "--indexed", BY_, "--", "deals", "stage",
        ],
    ),
    (
        "dataset.rename_column",
        {"dataset": "deals", "column": "a", "new_name": "b"},
        ["dataset", "rename-column", CFG, BY_, "--", "deals", "a", "b"],
    ),
    (
        "dataset.drop_column",
        {"dataset": "deals", "column": "a", "confirm": True},
        ["dataset", "drop-column", CFG, "--yes", BY_, "--", "deals", "a"],
    ),
    (
        "dataset.drop_column",
        {"dataset": "deals", "column": "a", "confirm": False},
        ["dataset", "drop-column", CFG, BY_, "--", "deals", "a"],
    ),
    (
        "dataset.set_flags",
        {"dataset": "deals", "column": "a", "filterable": True, "embed": False},
        ["dataset", "set-flags", CFG, "--filterable", "--no-embed", BY_, "--", "deals", "a"],
    ),
    (
        "dataset.set_flags",
        {
            "dataset": "deals", "column": "a", "filterable": False, "embed": True, "sensitive": False,
            "indexed": True, "label": "L", "source_name": "S",
        },
        [
            "dataset", "set-flags", CFG, "--no-filterable", "--embed", "--no-sensitive", "--indexed",
            "--label=L", "--source-name=S", BY_, "--", "deals", "a",
        ],
    ),
    (
        "dataset.set_choices",
        {"dataset": "deals", "column": "stage", "choices": ["x", "y"]},
        ["dataset", "set-choices", CFG, BY_, "--", "deals", "stage", "x,y"],
    ),
    ("dataset.set", {"dataset": "deals"}, ["dataset", "set", CFG, BY_, "--", "deals"]),
    (
        "dataset.set",
        {
            "dataset": "deals", "title": "T", "description": "D", "title_column": "name",
            "audiences": ["all"], "max_outdated_percent": 5, "history": True, "confirm": True,
        },
        [
            "dataset", "set", CFG, "--title=T", "--description=D", "--title-column=name",
            "--audiences=all", "--max-outdated-percent=5", "--history", "--yes", BY_, "--", "deals",
        ],
    ),
    (
        "dataset.set",
        {"dataset": "deals", "history": False, "title_column": ""},
        ["dataset", "set", CFG, "--title-column=", "--no-history", BY_, "--", "deals"],
    ),
]

DATASET_REFUSALS = [
    ("dataset.create", {"dataset": "Bad", "title": "T", "audiences": ["all"]}),
    ("dataset.create", {"dataset": "-x", "title": "T", "audiences": ["all"]}),
    ("dataset.create", {"dataset": "deals", "title": "T", "audiences": []}),
    ("dataset.create", {"dataset": "deals", "title": "T", "audiences": ["owners"]}),
    ("dataset.create", {"dataset": "deals", "title": "", "audiences": ["all"]}),
    ("dataset.create", {"dataset": "deals", "title": "a\nb", "audiences": ["all"]}),
    ("dataset.add_column", {"dataset": "deals", "column": "a", "type": "json"}),
    ("dataset.add_column", {"dataset": "deals", "column": "A", "type": "text"}),
    ("dataset.add_column", {"dataset": "deals", "column": "a", "type": "text", "filterable": "yes"}),
    ("dataset.add_column", {"dataset": "deals", "column": "a", "type": "choice", "choices": []}),
    ("dataset.add_column", {"dataset": "deals", "column": "a", "type": "choice", "choices": ["a,b"]}),
    ("dataset.add_column", {"dataset": "deals", "column": "a", "type": "choice",
                            "choices": [f"c{i}" for i in range(51)]}),
    ("dataset.add_column", {"dataset": "deals", "column": "a", "type": "ref", "ref": "Bad"}),
    ("dataset.add_column", {"dataset": "deals", "column": "a", "type": "text", "x": 1}),
    ("dataset.rename_column", {"dataset": "deals", "column": "a", "new_name": "--x"}),
    ("dataset.drop_column", {"dataset": "deals", "column": "a"}),
    ("dataset.drop_column", {"dataset": "deals", "column": "a", "confirm": "yes"}),
    ("dataset.set_flags", {"dataset": "deals", "column": "a", "embed": 1}),
    ("dataset.set_choices", {"dataset": "deals", "column": "a", "choices": "a,b"}),
    ("dataset.set", {"dataset": "deals", "max_outdated_percent": 101}),
    ("dataset.set", {"dataset": "deals", "max_outdated_percent": True}),
    ("dataset.set", {"dataset": "deals", "max_outdated_percent": -1}),
    ("dataset.set", {"dataset": "deals", "title_column": "Bad"}),
    ("dataset.set", {"dataset": "deals", "audiences": []}),
    ("dataset.set", {"dataset": "deals", "history": "on"}),
]


@pytest.mark.parametrize(("op", "args", "expected"), DATASET_EXAMPLES)
def test_dataset_op_argv(op, args, expected):
    valid = validate(op, args, False, None)
    assert not valid.op.is_config_op and valid.op.needs_by
    assert argv(valid, "/etc/kh.yaml", "a@b.test") == [
        "--config=/etc/kh.yaml" if t == CFG else t for t in expected
    ]


@pytest.mark.parametrize(("op", "args"), DATASET_REFUSALS)
def test_dataset_op_refusals(op, args):
    with pytest.raises(OpError):
        validate(op, args, False, None)


def test_dataset_ops_refuse_dry_run_and_sha():
    with pytest.raises(OpError):
        validate("dataset.set", {"dataset": "deals"}, True, None)
    with pytest.raises(OpError):
        validate("dataset.set", {"dataset": "deals"}, False, SHA)


def test_dataset_ops_need_the_signed_in_email():
    with pytest.raises(OpError):
        argv(validate("dataset.set", {"dataset": "deals"}, False, None), "/etc/kh.yaml")


@pytest.mark.parametrize(("op", "args", "expected"), DATASET_EXAMPLES)
def test_dataset_argv_parses_with_the_dataset_parser(op, args, expected):
    from knowledge_hub.datasets.cli import build_parser

    out = argv(validate(op, args, False, None), "/etc/kh.yaml", "a@b.test")
    assert out[0] == "dataset"
    ns = build_parser().parse_args(out[1:])
    assert ns.by == "a@b.test"
    assert str(ns.config) == "/etc/kh.yaml"


def test_ops_table_has_32_operations():
    assert len(OPS) == 32


def test_every_op_has_an_example():
    covered = {e[0] for e in EXAMPLES} | {e[0] for e in INPROC} | {e[0] for e in DATASET_EXAMPLES}
    assert covered == set(OPS)


@pytest.mark.parametrize(("op", "args", "dry", "sha", "expected"), EXAMPLES)
def test_valid_example_argv(op, args, dry, sha, expected):
    valid = validate(op, args, dry, sha)
    assert valid.name == op
    assert valid.args == args
    assert argv(valid, "/etc/kh.yaml", "a@b.test") == expected


@pytest.mark.parametrize(("op", "args"), INPROC)
def test_inproc_ops_validate_and_have_no_argv(op, args):
    valid = validate(op, args, False, None)
    assert valid.kind == "inproc"
    with pytest.raises(OpError):
        argv(valid, "/etc/kh.yaml")


GOOD = {"name": "ops", "description": "d"}
REFUSALS = [
    # unknown op
    ("nope", {}, False, None),
    ("", {}, False, None),
    (None, {}, False, None),
    (5, {}, False, None),
    # args shape
    ("label.archive", None, False, None),
    ("label.archive", [], False, None),
    ("label.archive", "ops", False, None),
    ("label.archive", {}, False, None),
    ("label.archive", {"name": "ops", "x": 1}, False, None),
    ("label.add", {"name": "ops"}, False, None),
    ("label.add", {"name": "ops", "description": "d", "dry_run": True}, False, None),
    ("health.index_check", {"x": 1}, False, None),
    ("jobs.reclaim", {"ids": [1]}, False, None),
    ("repo.set", {"name": "tech-a"}, False, None),
    # wrong types
    ("label.archive", {"name": 5}, False, None),
    ("label.archive", {"name": None}, False, None),
    ("label.archive", {"name": ["ops"]}, False, None),
    ("person.remove", {"login": "a", "from_groups": "yes"}, False, None),
    ("person.remove", {"login": "a", "from_groups": 1}, False, None),
    ("group.remove", {"name": "eng", "confirm": None}, False, None),
    ("group.add", {"name": "eng", "members": "a,b", "approvers": []}, False, None),
    ("group.add", {"name": "eng", "members": [1], "approvers": []}, False, None),
    ("repo.add", {"name": "tech-a", "dirs": "docs"}, False, None),
    ("jobs.retry", {"ids": "1"}, False, None),
    ("jobs.retry", {"ids": [True]}, False, None),
    ("jobs.retry", {"ids": ["1"]}, False, None),
    ("label.add", {"name": "ops", "description": 5}, False, None),
    # values failing their rules
    ("label.archive", {"name": "-x"}, False, None),
    ("label.archive", {"name": "--config=/x"}, False, None),
    ("label.archive", {"name": "has space"}, False, None),
    ("label.archive", {"name": "UPPER"}, False, None),
    ("label.archive", {"name": "a\nb"}, False, None),
    ("label.archive", {"name": "a" * 64}, False, None),
    ("label.archive", {"name": ""}, False, None),
    ("person.add", {"login": "-alice", "email": "a@b"}, False, None),
    ("person.add", {"login": "alice", "email": "no-at"}, False, None),
    ("person.add", {"login": "alice", "email": "a@b@c"}, False, None),
    ("person.add", {"login": "alice", "email": "@b"}, False, None),
    ("person.add", {"login": "alice", "email": "a@"}, False, None),
    ("person.add", {"login": "alice", "email": "a b@c"}, False, None),
    ("person.add", {"login": "alice", "email": "a" * 250 + "@b.com"}, False, None),
    ("label.add", {"name": "ops", "description": "x" * 201}, False, None),
    ("label.add", {"name": "ops", "description": "a\nb"}, False, None),
    ("label.add", {"name": "ops", "description": "a\x00b"}, False, None),
    ("label.describe", {"name": "ops", "description": "tab\there"}, False, None),
    ("group.add", {"name": "owners", "members": [], "approvers": []}, False, None),
    ("group.add", {"name": "eng-approve", "members": [], "approvers": []}, False, None),
    ("group.add", {"name": "eng", "members": ["-a"], "approvers": []}, False, None),
    ("group.add", {"name": "eng", "members": ["a"] * 51, "approvers": []}, False, None),
    ("group.archive", {"name": "owners"}, False, None),
    ("member.add", {"group": "-g", "login": "bob"}, False, None),
    ("member.add", {"group": "eng", "login": "b ob"}, False, None),
    ("repo.add", {"name": "tech-a", "dirs": ["-d"]}, False, None),
    ("repo.add", {"name": "tech-a", "dirs": ["a,b"]}, False, None),
    ("repo.add", {"name": "tech-a", "dirs": ["../x"]}, False, None),
    ("repo.add", {"name": "tech-a", "labels": ["--x"]}, False, None),
    ("repo.add", {"name": "tech-a", "labels": ["l"] * 51}, False, None),
    ("repo.remove", {"name": "--yes", "confirm": True}, False, None),
    ("repo.rebuild", {"repo": "-r"}, False, None),
    ("jobs.retry", {"ids": [0]}, False, None),
    ("jobs.retry", {"ids": [-1]}, False, None),
    ("jobs.retry", {"ids": list(range(1, 102))}, False, None),
    ("jobs.retry", {"ids": []}, False, None),
    # dry_run
    ("jobs.retry", {}, True, None),
    ("jobs.retry", {"ids": [1]}, True, None),
    ("jobs.reclaim", {}, True, None),
    ("repo.rebuild", {"repo": "kb-eng"}, True, None),
    ("embeddings.requeue", {}, True, None),
    ("health.setup_plan", {}, True, None),
    ("health.index_check", {}, True, None),
    ("label.archive", {"name": "ops"}, "true", None),
    ("label.archive", {"name": "ops"}, 1, None),
    ("label.archive", {"name": "ops"}, None, None),
    # config_sha256
    ("label.archive", {"name": "ops"}, False, "A" * 64),
    ("label.archive", {"name": "ops"}, False, "a" * 63),
    ("label.archive", {"name": "ops"}, False, "a" * 65),
    ("label.archive", {"name": "ops"}, False, "g" * 64),
    ("label.archive", {"name": "ops"}, False, 5),
    ("label.archive", {"name": "ops"}, False, ""),
    ("jobs.reclaim", {}, False, SHA),
    ("repo.rebuild", {"repo": "kb-eng"}, False, SHA),
    ("health.setup_plan", {}, False, SHA),
    ("health.index_check", {}, False, SHA),
]


def test_refusal_table_is_large_enough():
    assert len(REFUSALS) >= 40


@pytest.mark.parametrize(("op", "args", "dry", "sha"), REFUSALS)
def test_refusals(op, args, dry, sha):
    with pytest.raises(OpError) as exc:
        validate(op, args, dry, sha)
    assert exc.value.reason


def test_boundaries_are_accepted():
    validate("label.add", {"name": "ops", "description": "x" * 200}, False, None)
    validate("label.add", {"name": "ops", "description": ""}, False, None)
    validate("group.add", {"name": "eng", "members": ["a"] * 50, "approvers": []}, False, None)
    validate("jobs.retry", {"ids": list(range(1, 101))}, False, None)
    validate("person.add", {"login": "a", "email": "a@" + "b" * 250}, False, None)
    assert OpError("why").reason == "why"


# ---- round trip through the real admin parser ----


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kh")
    sub = p.add_subparsers(dest="command", required=True)
    for name in COMMAND_MODULES:
        importlib.import_module(f"knowledge_hub.admin.{name}").register(sub)
    return p


def split(text):
    return [t for t in text.split(",") if t]


def parsed(op, ns):
    """The namespace as the validated args of ``op``."""
    if op in ("label.add", "label.describe"):
        return {"name": ns.name, "description": ns.description if op == "label.add" else ns.text}
    if op.startswith("label."):
        return {"name": ns.name}
    if op == "person.add":
        return {"login": ns.login, "email": ns.email}
    if op == "person.remove":
        return {"login": ns.login, "from_groups": ns.from_groups}
    if op == "group.add":
        return {"name": ns.name, "members": split(ns.members), "approvers": split(ns.approvers)}
    if op == "group.remove":
        return {"name": ns.name, "confirm": ns.yes}
    if op.startswith("group."):
        return {"name": ns.name}
    if op == "member.remove":
        return {"group": ns.group, "login": ns.login, "with_approver": ns.with_approver}
    if op in ("member.add", "approver.add", "approver.remove"):
        return {"group": ns.group, "login": ns.login}
    if op == "repo.remove":
        return {"name": ns.name, "confirm": ns.yes}
    out = {"name": ns.name}
    if ns.dirs is not None:
        out["dirs"] = split(ns.dirs)
    if ns.labels is not None:
        out["labels"] = split(ns.labels)
    return out


HOSTILE = [
    ("person.add", {"login": "alice", "email": "--config=/x@y"}),
    ("person.add", {"login": "alice", "email": "-x@y"}),
    ("label.add", {"name": "ops", "description": "-x"}),
    ("label.add", {"name": "ops", "description": "--config=/x"}),
    ("label.add", {"name": "ops", "description": "--"}),
    ("label.describe", {"name": "ops", "description": "-x"}),
    ("label.describe", {"name": "ops", "description": "--dry-run"}),
]


@pytest.mark.parametrize(
    ("op", "args", "dry", "sha"),
    [(e[0], e[1], e[2], e[3]) for e in EXAMPLES if not e[0].startswith(("health.", "item."))]
    + [(op, args, True, SHA) for op, args in HOSTILE]
    + [(op, args, False, None) for op, args in HOSTILE],
)
def test_argv_round_trips_through_the_real_parser(op, args, dry, sha):
    valid = validate(op, args, dry, sha)
    ns = parser().parse_args(argv(valid, "/etc/kh.yaml"))
    assert parsed(op, ns) == valid.args
    assert str(ns.config) == "/etc/kh.yaml"
    assert ns.dry_run is dry
    assert ns.if_sha256 == sha
    assert ns.no_apply is False


def test_every_config_op_is_round_tripped():
    cli_ops = {n for n, o in OPS.items() if o.is_config_op}
    assert cli_ops == {e[0] for e in EXAMPLES if not e[0].startswith(("health.", "item."))}


def test_ops_module_exports():
    assert ops.OPS is OPS


# ---- item.move ----

MOVE_ARGS = {"id": MOVE_ID, "from_group": "mgmt", "to_group": "all"}


def test_item_move_round_trips_through_the_real_parser():
    valid = validate("item.move", MOVE_ARGS, False, None)
    ns = parser().parse_args(argv(valid, "/etc/kh.yaml", "a@b.test"))
    assert ns.id == MOVE_ID
    assert ns.from_group == "mgmt"
    assert ns.to_group == "all"
    assert ns.by == "a@b.test"
    assert str(ns.config) == "/etc/kh.yaml"


def test_item_move_is_not_a_config_op():
    assert OPS["item.move"].is_config_op is False
    assert OPS["item.move"].needs_by is True


def test_item_move_refuses_by_in_args():
    with pytest.raises(OpError, match="unexpected argument: by"):
        validate("item.move", {**MOVE_ARGS, "by": "x@y.test"}, False, None)


def test_item_move_refuses_dry_run_and_config_sha256():
    with pytest.raises(OpError, match="dry_run is not available"):
        validate("item.move", MOVE_ARGS, True, None)
    with pytest.raises(OpError, match="config_sha256 is not available"):
        validate("item.move", MOVE_ARGS, False, SHA)


def test_item_move_refuses_a_bad_id():
    with pytest.raises(OpError, match="item id is not valid"):
        validate("item.move", {**MOVE_ARGS, "id": "rec:x"}, False, None)


def test_item_move_needs_the_signed_in_email():
    valid = validate("item.move", MOVE_ARGS, False, None)
    with pytest.raises(OpError, match="signed-in email is missing"):
        argv(valid, "/c.yaml")
    assert argv(valid, "/c.yaml", "a@b.test") == [
        "move",
        "--config=/c.yaml",
        "--from=mgmt",
        "--to=all",
        "--by=a@b.test",
        "--",
        MOVE_ID,
    ]
