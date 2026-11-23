"""kh dataset bi-role: read-only per-group logins for BI tools (plan 72, 3.9, p11)."""

from __future__ import annotations

import json
import re

import psycopg
import pytest
from psycopg import errors

from knowledge_hub.datasets import bi, cli
from knowledge_hub.datasets.model import DatasetError
from tests.support.datasets import make_dataset, migrator_dsn
from tests.support.pg import FRESH_DB
from tests.support.principals import KH_CONFIG

pytestmark = pytest.mark.integration

ADMIN = "admin@example.test"
CONFIG = KH_CONFIG.model_copy(update={"admins": [ADMIN]})
ROLE_LINE = re.compile(r"^kh_bi_[a-z2-7]{10}$")


@pytest.fixture(autouse=True)
def deals(seeded_db, fresh_db):
    ds = make_dataset(fresh_db)
    with psycopg.connect(migrator_dsn(fresh_db)) as conn:
        for key, audience in (("a", "all"), ("b", "all"), ("c", "mgmt")):
            conn.execute(
                "INSERT INTO kh_data.deals (_key, _audience, _source_updated_at, name)"
                " VALUES (%s, %s, now(), %s)",
                (key, audience, f"deal {key}"),
            )
    return ds


@pytest.fixture
def admin_dsn(fresh_db):
    return fresh_db.admin_dsn(FRESH_DB)


@pytest.fixture
def settings(db_settings, admin_dsn, tmp_path):
    f = tmp_path / "admin-dsn"
    f.write_text(admin_dsn + "\n")
    return db_settings.model_copy(update={"bi_enabled": True, "setup_pg_admin_dsn_file": f})


@pytest.fixture
def run(settings, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_config", lambda path: CONFIG)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    def go(*args: str) -> tuple[int, str]:
        code = cli.main(["bi-role", *args])
        return code, capsys.readouterr().out.strip()

    return go


def _create(run, group="all"):
    code, out = run("create", group, "--by", ADMIN, "--config", "kh.yaml")
    assert code == 0, out
    lines = out.splitlines()
    assert len(lines) == 2 and lines[0].startswith("role ") and lines[1].startswith("password ")
    return lines[0].removeprefix("role "), lines[1].removeprefix("password ")


def _as(fresh_db, role):
    return psycopg.connect(fresh_db.dsn(role, FRESH_DB), autocommit=True)


def _drop_quietly(admin_dsn, role):
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f"DROP ROLE IF EXISTS {role}")


def test_bi_login_sees_only_its_group(run, fresh_db, admin_dsn):
    role, password = _create(run)
    try:
        assert ROLE_LINE.fullmatch(role) and len(password) >= 32
        with _as(fresh_db, role) as conn:
            assert conn.execute("SELECT _key FROM kh_data.deals ORDER BY _key").fetchall() == [("a",), ("b",)]
            conn.execute("SET kh.audiences = 'mgmt'")
            conn.execute("SET ROLE kh_bi")
            assert conn.execute("SELECT session_user").fetchone() == (role,)
            assert conn.execute("SELECT _key FROM kh_data.deals ORDER BY _key").fetchall() == [("a",), ("b",)]
            with pytest.raises(errors.ReadOnlySqlTransaction):
                conn.execute("INSERT INTO kh_data.deals (_key, _audience, _source_updated_at)"
                             " VALUES ('z', 'all', now())")
            conn.execute("SET default_transaction_read_only = off")  # a session may; only SELECT is granted
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute("INSERT INTO kh_data.deals (_key, _audience, _source_updated_at)"
                             " VALUES ('z', 'all', now())")
        with _as(fresh_db, role) as conn:
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute("SELECT count(*) FROM kh.records")
            assert conn.execute("SHOW statement_timeout").fetchone() == ("30s",)
            assert conn.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
        code, out = run("list")
        assert code == 0 and out.startswith(f"{role}\tall\t")
        code, out = run("list", "--json")
        assert code == 0 and json.loads(out)[0]["role"] == role
        assert json.loads(out)[0]["created_by"] == ADMIN
    finally:
        _drop_quietly(admin_dsn, role)


def test_mgmt_login_sees_mgmt_rows(run, fresh_db, admin_dsn):
    role, _ = _create(run, "mgmt")
    try:
        with _as(fresh_db, role) as conn:
            assert conn.execute("SELECT _key FROM kh_data.deals").fetchall() == [("c",)]
    finally:
        _drop_quietly(admin_dsn, role)


def test_rotate_changes_password_hash(run, fresh_db, admin_dsn):
    role, password = _create(run)
    try:
        def hash_():
            with psycopg.connect(admin_dsn) as conn:
                sql = "SELECT rolpassword FROM pg_authid WHERE rolname = %s"
                return conn.execute(sql, (role,)).fetchone()

        before = hash_()
        code, out = run("rotate", role, "--by", ADMIN, "--config", "kh.yaml")
        assert code == 0
        lines = out.splitlines()
        assert lines[0] == f"role {role}" and lines[1].startswith("password ")
        assert lines[1] != f"password {password}"
        assert hash_() != before
    finally:
        _drop_quietly(admin_dsn, role)


def test_drop_terminates_sessions_and_removes_role_and_row(run, fresh_db, admin_dsn):
    role, _ = _create(run)
    try:
        live = _as(fresh_db, role)
        assert live.execute("SELECT 1").fetchone() == (1,)
        assert run("drop", role, "--by", ADMIN, "--config", "kh.yaml")[0] == 2  # needs --yes
        code, out = run("drop", role, "--by", ADMIN, "--config", "kh.yaml", "--yes")
        assert (code, out) == (0, f"dataset: dropped {role}")
        with pytest.raises(psycopg.OperationalError):
            live.execute("SELECT 1")
        with psycopg.connect(admin_dsn) as conn:
            sql = "SELECT count(*) FROM pg_roles WHERE rolname = %s"
            assert conn.execute(sql, (role,)).fetchone() == (0,)
            assert conn.execute("SELECT count(*) FROM kh.bi_roles").fetchone() == (0,)
    finally:
        _drop_quietly(admin_dsn, role)


def test_other_roles_are_not_bi_roles(run):
    for name in ("kh_app", "kh_bi_aaaaaaaaaa"):
        for cmd in ("rotate", "drop"):
            extra = ("--yes",) if cmd == "drop" else ()
            code, out = run(cmd, name, "--by", ADMIN, "--config", "kh.yaml", *extra)
            assert (code, out) == (1, f"dataset: {name} is not a BI role")


def test_create_for_unknown_group_is_refused(run):
    code, out = run("create", "nope", "--by", ADMIN, "--config", "kh.yaml")
    assert (code, out) == (1, "dataset: nope is not an active group")


def test_failed_mapping_drops_the_role(settings, admin_dsn, monkeypatch):
    # the migrator DSN cannot insert: the role must not survive
    with psycopg.connect(admin_dsn) as conn:
        conn.execute("REVOKE INSERT ON kh.bi_roles FROM kh_migrator")
        conn.execute("ALTER TABLE kh.bi_roles OWNER TO postgres")
    try:
        with pytest.raises(psycopg.Error):
            bi.create_bi_role(admin_dsn, settings.dsn("migrator"), "all", ADMIN)
        with psycopg.connect(admin_dsn) as conn:
            assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname ~ '^kh_bi_'").fetchone() == (0,)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            for (r,) in conn.execute("SELECT rolname FROM pg_roles WHERE rolname ~ '^kh_bi_'").fetchall():
                conn.execute(f"DROP ROLE {r}")


def test_disabled_and_missing_admin_dsn(run, settings, monkeypatch, capsys):
    off = settings.model_copy(update={"bi_enabled": False})
    monkeypatch.setattr(cli, "load_settings", lambda: off)
    for args in (("list",), ("create", "all", "--by", ADMIN, "--config", "kh.yaml")):
        code = cli.main(["bi-role", *args])
        assert (code, capsys.readouterr().out.strip()) == (
            1, "dataset: bi roles are disabled; set KH_BI_ENABLED=true")
    nodsn = settings.model_copy(update={"setup_pg_admin_dsn_file": None})
    monkeypatch.setattr(cli, "load_settings", lambda: nodsn)
    code = cli.main(["bi-role", "create", "all", "--by", ADMIN, "--config", "kh.yaml"])
    assert (code, capsys.readouterr().out.strip()) == (1, "dataset: KH_SETUP_PG_ADMIN_DSN_FILE is required")


def test_bi_error_type():
    with pytest.raises(DatasetError):
        bi._check_role("host=/nonexistent", "kh_app")
