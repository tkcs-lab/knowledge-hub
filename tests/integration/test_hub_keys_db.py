"""Hub keys against Postgres: create, verify, list, revoke (plan 3.7)."""

from __future__ import annotations

import json
import os
import stat

import psycopg
import pytest

from knowledge_hub.auth import keys_cli
from knowledge_hub.auth.keys import KEY_RE, KEY_TOOLS, create_key, verify_key
from knowledge_hub.config import GroupConfig, KhConfig
from tests.support.pg import FRESH_DB

CFG = KhConfig(
    people={"alice": "alice@example.test"},
    groups={
        "all": GroupConfig(members=["alice"], approvers=["alice"]),
        "old": GroupConfig(members=["alice"], approvers=["alice"], archived=True),
    },
)


@pytest.fixture
def cli(db_settings, monkeypatch, capsys):
    monkeypatch.setattr(keys_cli, "load_settings", lambda: db_settings)
    monkeypatch.setattr(keys_cli, "load_config", lambda _path: CFG)

    def run(*argv):
        code = keys_cli.main(list(argv))
        return code, capsys.readouterr().out

    return run


@pytest.fixture
def admin(fresh_db):
    with psycopg.connect(fresh_db.admin_dsn(FRESH_DB), autocommit=True) as conn:
        yield conn


async def _create(fresh_db, group="all", tools=("search",), name="n"):
    async with await psycopg.AsyncConnection.connect(fresh_db.dsn("kh_migrator", FRESH_DB)) as conn:
        key = await create_key(conn, group=group, tools=list(tools), name=name, created_by="test")
        await conn.commit()
    return key


def _id(key):
    return key.split(".")[0][4:]


async def test_create_then_verify(fresh_db, app_pool):
    key = await _create(fresh_db, tools=("search", "usage"))
    assert KEY_RE.fullmatch(key)
    caller = await verify_key(app_pool, CFG, key)
    assert caller is not None
    assert caller.sub == f"key:{_id(key)}"
    assert caller.principal_type == "n8n_workflow"
    assert caller.email is None
    assert caller.audiences == frozenset({"all"})
    assert caller.tools == frozenset({"search", "usage"})
    assert caller.token_id == _id(key)


async def test_wrong_secret_gives_none(fresh_db, app_pool):
    key = await _create(fresh_db)
    other = key.split(".")[0] + "." + "A" * 43
    assert await verify_key(app_pool, CFG, other) is None
    assert await verify_key(app_pool, CFG, "not a key") is None


async def test_revoked_gives_none(fresh_db, app_pool, admin):
    key = await _create(fresh_db)
    admin.execute("UPDATE kh.api_keys SET revoked_at = now() WHERE id = %s", (_id(key),))
    assert await verify_key(app_pool, CFG, key) is None


async def test_expired_gives_none(fresh_db, app_pool, admin):
    key = await _create(fresh_db)
    admin.execute(
        "UPDATE kh.api_keys SET expires_at = now() - interval '1 second' WHERE id = %s", (_id(key),)
    )
    assert await verify_key(app_pool, CFG, key) is None


async def test_expires_days_sets_expiry(fresh_db, admin):
    async with await psycopg.AsyncConnection.connect(fresh_db.dsn("kh_migrator", FRESH_DB)) as conn:
        key = await create_key(conn, group="all", tools=["search"], name="n", created_by="t", expires_days=2)
        await conn.commit()
    row = admin.execute(
        "SELECT expires_at > now() + interval '47 hours' AND expires_at < now() + interval '49 hours'"
        " FROM kh.api_keys WHERE id = %s",
        (_id(key),),
    ).fetchone()
    assert row == (True,)


@pytest.mark.parametrize("group", ["nope", "old"])
async def test_unknown_or_archived_group_gives_none(fresh_db, app_pool, group):
    key = await _create(fresh_db, group=group)
    assert await verify_key(app_pool, CFG, key) is None


async def test_last_used_moves_once_a_minute(fresh_db, app_pool, admin):
    key = await _create(fresh_db)
    kid = _id(key)

    def last():
        return admin.execute("SELECT last_used_at FROM kh.api_keys WHERE id = %s", (kid,)).fetchone()[0]

    assert last() is None
    assert await verify_key(app_pool, CFG, key) is not None
    first = last()
    assert first is not None
    assert await verify_key(app_pool, CFG, key) is not None
    assert last() == first
    admin.execute("UPDATE kh.api_keys SET last_used_at = now() - interval '2 minutes' WHERE id = %s", (kid,))
    assert await verify_key(app_pool, CFG, key) is not None
    assert admin.execute(
        "SELECT last_used_at > now() - interval '1 minute' FROM kh.api_keys WHERE id = %s", (kid,)
    ).fetchone() == (True,)


def test_create_prints_three_lines_and_secret_once(cli, admin):
    code, out = cli("create", "--group", "all", "--tools", "search,usage", "--name", "nightly")
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("created key ") and lines[0].endswith(" for group all")
    assert lines[1].startswith("key: khk_")
    assert lines[2] == "shown once: store it as a secret"
    key = lines[1][len("key: ") :]
    assert lines[0].split()[2] == _id(key)
    secret = key.split(".", 1)[1]
    code, listing = cli("list")
    assert code == 0 and secret not in listing
    code, js = cli("list", "--json")
    assert secret not in js and "secret" not in js and "sha256" not in js
    stored = admin.execute("SELECT created_by, tools FROM kh.api_keys").fetchone()
    assert stored[0].startswith("cli:")
    assert stored[1] == ["search", "usage"]


def test_default_tools(cli, admin):
    assert cli("create", "--group", "all", "--name", "n")[0] == 0
    assert admin.execute("SELECT tools FROM kh.api_keys").fetchone()[0] == ["ingest_record", "search"]


@pytest.mark.parametrize(
    ("argv", "text"),
    [
        (["--group", "old", "--name", "n"], "key: group old is not an active group"),
        (["--group", "nope", "--name", "n"], "key: group nope is not an active group"),
        (["--group", "all", "--tools", "bogus", "--name", "n"], "key: unknown tool bogus"),
        (
            ["--group", "all", "--tools", "move_item", "--name", "n"],
            "key: tool move_item cannot be given to a key",
        ),
        (["--group", "all", "--name", "_bad"], "key: name _bad is not valid"),
    ],
)
def test_create_refusals(cli, admin, argv, text):
    code, out = cli("create", *argv)
    assert code == 1
    assert out.strip() == text
    assert admin.execute("SELECT count(*) FROM kh.api_keys").fetchone() == (0,)


def test_create_out_file_mode_0600(cli, tmp_path):
    target = tmp_path / "k"
    code, out = cli("create", "--group", "all", "--name", "n", "--out", str(target))
    assert code == 0
    assert f"key written to {target}" in out
    assert "khk_" not in out
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert KEY_RE.fullmatch(target.read_text().strip().removeprefix("key: "))


def test_list_header_and_status(cli, admin):
    cli("create", "--group", "all", "--name", "a")
    cli("create", "--group", "all", "--name", "b")
    ids = [r[0] for r in admin.execute("SELECT id FROM kh.api_keys ORDER BY created_at, id").fetchall()]
    admin.execute("UPDATE kh.api_keys SET expires_at = now() - interval '1 day' WHERE id = %s", (ids[0],))
    assert cli("revoke", ids[1])[1].strip() == f"revoked {ids[1]}"
    code, out = cli("list")
    lines = out.splitlines()
    assert lines[0] == "id\tname\tgroup\ttools\texpires\tlast_used\tstatus"
    by_id = {line.split("\t")[0]: line.split("\t") for line in lines[1:]}
    assert by_id[ids[0]][-1] == "expired"
    assert by_id[ids[1]][-1] == "revoked"
    rows = json.loads(cli("list", "--json")[1])
    assert {r["id"] for r in rows} == set(ids)
    assert set(rows[0]) == {"id", "name", "group", "tools", "expires", "last_used", "status"}


def test_revoke_unknown_exits_1(cli):
    code, out = cli("revoke", "aaaaaaaaaa")
    assert code == 1
    assert out.strip() == "key: aaaaaaaaaa not found"


def test_key_tools_are_creatable(cli):
    assert cli("create", "--group", "all", "--tools", ",".join(KEY_TOOLS), "--name", "all")[0] == 0
