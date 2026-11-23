import psycopg
import pytest

from knowledge_hub.config import GroupConfig, KhConfig
from knowledge_hub.db.migrate import pending
from knowledge_hub.settings import Settings
from knowledge_hub.setup.labels import LabelsStep
from knowledge_hub.setup.postgres import PostgresStep
from knowledge_hub.setup.steps import run
from tests.support.pg import Cluster
from tests.support.seed import add_doc, add_record

DB = "kh_setup"
ROLES = ("kh_migrator", "kh_app", "kh_indexer")


@pytest.fixture(scope="module")
def cluster():
    c = Cluster()
    # the session cluster of the same process owns the default directory
    c.root = c.root.with_name(f"{c.root.name}-setup")
    c.data, c.sock = c.root / "data", c.root / "sock"
    c.start()
    try:
        yield c
    finally:
        c.stop()


@pytest.fixture
def empty(cluster):
    """The cluster without the kh roles and database."""
    with psycopg.connect(cluster.admin_dsn(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        for role in ROLES:
            conn.execute(f"DROP ROLE IF EXISTS {role}")
        conn.execute("DROP ROLE IF EXISTS kh_bi")
    return cluster


@pytest.fixture
def settings(cluster, tmp_path):
    files = {}
    for role in ("migrator", "app", "indexer"):
        f = tmp_path / role
        f.write_text(f"pw-{role}\n")
        files[f"db_{role}_password_file"] = f
    return Settings(db_host=str(cluster.sock), db_name=DB, **files)


def cfg(groups=("all",), labels=None):
    return KhConfig(
        people={"alice": "a@example.test"},
        groups={g: GroupConfig(members=["alice"], approvers=["alice"]) for g in groups},
        labels=labels or {},
    )


def step(cluster, settings, config=None, admin=True):
    return PostgresStep(config or cfg(), settings, cluster.admin_dsn() if admin else None)


def lines(changes):
    return [c.render() for c in changes]


def prefixes(changes, kinds="+~-!"):
    return [c.render() for c in changes if c.prefix in kinds]


def mig(cluster):
    # the migrator has no password in the socket cluster (trust auth)
    return cluster.dsn("kh_migrator", DB)


async def test_plan_apply_converge(empty, settings, capsys):
    s = step(empty, settings)
    planned = lines(await s.plan())
    for expected in ("+ role kh_migrator", "+ role kh_app", "+ role kh_indexer", "+ role kh_bi",
                     f"+ database {DB}", "+ extension vector", "+ migration 0001_baseline.sql",
                     "+ migration 0002_usage_events.sql", "+ migration 0003_supersede.sql",
                     "+ migration 0005_move_record.sql", "+ migration 0006_identity.sql",
                     "+ migration 0007_datasets.sql",
                     "+ group all"):
        assert expected in planned
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(mig(empty))  # plan changed nothing
    assert await run([s], "apply", False) == 0
    assert prefixes(await s.plan()) == []
    assert pending(mig(empty)) == []
    with psycopg.connect(empty.admin_dsn()) as conn:
        rows = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls, rolpassword IS NOT NULL OR true FROM pg_roles "
            "WHERE rolname = ANY(%s)", (list(ROLES),)
        ).fetchall()
        assert len(rows) == 3 and all(r[0] and not r[1] and not r[2] for r in rows)
        assert conn.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (DB,)
        ).fetchone() == ("kh_migrator",)
        # passwords are re-set on every apply
        before = conn.execute("SELECT rolpassword FROM pg_authid WHERE rolname='kh_app'").fetchone()
    settings.db_app_password_file.write_text("changed")
    await step(empty, settings).apply(False)
    with psycopg.connect(empty.admin_dsn()) as conn:
        after = conn.execute("SELECT rolpassword FROM pg_authid WHERE rolname='kh_app'").fetchone()
    assert before != after


async def test_add_group_and_prune(empty, settings):
    await step(empty, settings).apply(False)
    config = cfg(("all", "extra"))
    s = step(empty, settings, config)
    assert prefixes(await s.plan()) == ["+ group extra"]
    await s.apply(False)
    assert prefixes(await s.plan()) == []
    # a config without "extra": unmanaged, kept without prune
    small = step(empty, settings)
    assert lines(await small.plan()) == ["? unmanaged group extra"]
    assert lines(await small.apply(False)) == ["? unmanaged group extra"]
    with psycopg.connect(mig(empty), autocommit=True) as conn:
        add_doc(conn, repo="kb-extra", audience="extra")
    assert lines(await small.apply(True)) == ["- group extra"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT count(*) FROM kh.documents").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM kh.groups").fetchone() == (1,)


async def test_prune_leaves_record_moves(empty, settings):
    await step(empty, settings, cfg(("all", "extra"))).apply(False)
    with psycopg.connect(mig(empty), autocommit=True) as conn:
        conn.execute("INSERT INTO kh.record_moves (audience, source, source_ref) VALUES ('extra', 's', 'r')")
    assert lines(await step(empty, settings).apply(True)) == ["- group extra"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT count(*) FROM kh.groups").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM kh.record_moves").fetchone() == (1,)


async def test_prune_refused_with_records(empty, settings):
    await step(empty, settings, cfg(("all", "extra"))).apply(False)
    with psycopg.connect(mig(empty), autocommit=True) as conn:
        add_record(conn, audience="extra")
    out = lines(await step(empty, settings).apply(True))
    assert out == ["! group extra: still has 1 records"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT count(*) FROM kh.groups").fetchone() == (2,)


async def test_changed_repo_is_refused(empty, settings, capsys):
    await step(empty, settings).apply(False)
    config = cfg()
    config.groups["all"].repo = "other-repo"
    s = step(empty, settings, config)
    out = prefixes(await s.plan())
    assert len(out) == 1 and out[0].startswith("! group all:")
    assert await run([s], "apply", False) == 2
    assert await run([s], "plan", False) == 1


async def test_without_admin_dsn(empty, settings):
    s = step(empty, settings, admin=False)
    out = lines(await s.plan())
    assert any(line.startswith("! role kh_migrator") for line in out)
    assert any(line.startswith(f"! database {DB}") for line in out)
    assert lines(await s.apply(False)) == out
    with psycopg.connect(empty.admin_dsn()) as conn:  # nothing was created
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'kh_%%'").fetchone() == (0,)
    # roles and database exist, extension and migrations do not
    with psycopg.connect(empty.admin_dsn(), autocommit=True) as conn:
        for r in ROLES:
            conn.execute(f"CREATE ROLE {r} LOGIN NOSUPERUSER NOBYPASSRLS")
        conn.execute("CREATE ROLE kh_bi NOLOGIN NOSUPERUSER NOBYPASSRLS")
        conn.execute(f"CREATE DATABASE {DB} OWNER kh_migrator")
    out = lines(await s.plan())
    assert out == ["! extension vector"]
    assert lines(await s.apply(False)) == out
    with psycopg.connect(empty.admin_dsn(DB), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector")
    assert "+ migration 0001_baseline.sql" in lines(await s.plan())
    await s.apply(False)
    assert prefixes(await s.plan()) == []


async def test_labels(empty, settings):
    await step(empty, settings, cfg(("all", "hr"))).apply(False)
    config = cfg(("all", "hr"), {"a": "first", "b": "second"})
    s = LabelsStep(config, mig(empty))
    assert lines(await s.plan()) == ["+ label a", "+ label b"]
    out = lines(await s.apply(False))
    assert out[:2] == ["+ label a", "+ label b"]
    assert sorted(out[2:]) == ["+ job git_push: kb-all", "+ job git_push: kb-hr"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute(
            "SELECT repo FROM kh.index_jobs WHERE kind='git_push' ORDER BY repo"
        ).fetchall() == [("kb-all",), ("kb-hr",)]
    assert await s.plan() == []
    # description update: no new jobs
    config.labels["a"] = "changed"
    assert lines(await s.plan()) == ["~ label a"]
    assert lines(await s.apply(False)) == ["~ label a"]
    # unmanaged: kept without prune, removed when unused
    small = LabelsStep(cfg(("all", "hr"), {"a": "changed"}), mig(empty))
    assert lines(await small.plan()) == ["? unmanaged label b"]
    assert lines(await small.apply(False)) == ["? unmanaged label b"]
    assert lines(await small.apply(True)) == ["- label b"]
    # used: refused
    with psycopg.connect(mig(empty), autocommit=True) as conn:
        add_doc(conn, labels=["a"])
    none = LabelsStep(cfg(("all", "hr")), mig(empty))
    out = lines(await none.apply(True))
    assert len(out) == 1 and out[0].startswith("! label a")
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT count(*) FROM kh.labels").fetchone() == (1,)


def cfg_archived(archived=("hr",), labels=None):
    config = cfg(("all", "hr"), labels)
    for g in archived:
        config.groups[g].archived = True
    return config


async def test_archived_group_is_kept_by_prune(empty, settings):
    await step(empty, settings, cfg(("all", "hr"))).apply(False)
    s = step(empty, settings, cfg_archived())
    assert lines(await s.plan()) == []
    assert lines(await s.apply(True)) == []
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT name FROM kh.groups ORDER BY name").fetchall() == [("all",), ("hr",)]


async def test_labels_jobs_skip_archived_groups(empty, settings):
    await step(empty, settings, cfg(("all", "hr"))).apply(False)
    config = cfg_archived(labels={"a": "first"})
    out = lines(await LabelsStep(config, mig(empty)).apply(False))
    assert out == ["+ label a", "+ job git_push: kb-all"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute(
            "SELECT repo FROM kh.index_jobs WHERE kind='git_push'"
        ).fetchall() == [("kb-all",)]


async def test_remove_label_deletes_unused_label_and_refuses_used(empty, settings):
    await step(empty, settings).apply(False)
    s = LabelsStep(cfg(labels={"gone": "x", "kept": "y", "rec": "z"}), mig(empty))
    await s.apply(False)
    with psycopg.connect(mig(empty), autocommit=True) as conn:
        add_doc(conn, labels=["kept"])
        add_record(conn, audience="all", labels=["rec"])
    assert lines([await s.remove_label("gone", write=True)]) == ["- label gone"]
    assert lines([await s.remove_label("kept", write=True)]) == ["! label kept: in use"]
    assert lines([await s.remove_label("rec", write=True)]) == ["! label rec: in use"]
    assert lines([await s.remove_label("gone", write=True)]) == ["i label gone: not in the database"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT name FROM kh.labels ORDER BY name").fetchall() == [("kept",), ("rec",)]


async def test_remove_label_plan_only_writes_nothing(empty, settings):
    await step(empty, settings).apply(False)
    s = LabelsStep(cfg(labels={"gone": "x"}), mig(empty))
    await s.apply(False)
    assert lines([await s.remove_label("gone", write=False)]) == ["- label gone"]
    assert lines([await s.remove_label("nope", write=False)]) == ["i label nope: not in the database"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT name FROM kh.labels").fetchall() == [("gone",)]


async def test_sync_groups_adds_only_groups(empty, settings):
    await step(empty, settings).apply(False)
    s = step(empty, settings, cfg(("all", "extra")))
    assert lines(await s.sync_groups(write=False)) == ["+ group extra"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT count(*) FROM kh.groups").fetchone() == (1,)
    assert lines(await s.sync_groups(write=True)) == ["+ group extra"]
    assert lines(await s.sync_groups(write=True)) == []
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT name FROM kh.groups ORDER BY name").fetchall() == [("all",), ("extra",)]


async def test_remove_group_refuses_records(empty, settings):
    await step(empty, settings, cfg(("all", "extra"))).apply(False)
    with psycopg.connect(mig(empty), autocommit=True) as conn:
        add_record(conn, audience="extra")
    s = step(empty, settings)
    assert lines([await s.remove_group("extra", write=True)]) == ["! group extra: still has 1 records"]
    assert lines([await s.remove_group("extra", write=False)]) == ["! group extra: still has 1 records"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT count(*) FROM kh.groups").fetchone() == (2,)


async def test_remove_group_deletes_documents_and_record_moves(empty, settings):
    await step(empty, settings, cfg(("all", "extra"))).apply(False)
    with psycopg.connect(mig(empty), autocommit=True) as conn:
        add_doc(conn, repo="kb-extra", audience="extra")
        conn.execute("INSERT INTO kh.record_moves (audience, source, source_ref) VALUES ('extra', 's', 'r')")
    s = step(empty, settings)
    assert lines([await s.remove_group("extra", write=False)]) == ["- group extra"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT count(*) FROM kh.groups").fetchone() == (2,)
    assert lines([await s.remove_group("extra", write=True)]) == ["- group extra"]
    assert lines([await s.remove_group("extra", write=True)]) == ["i group extra: not in the database"]
    with psycopg.connect(mig(empty)) as conn:
        assert conn.execute("SELECT count(*) FROM kh.documents").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM kh.record_moves").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM kh.groups").fetchone() == (1,)


async def test_group_usage_counts(empty, settings):
    await step(empty, settings, cfg(("all", "extra"))).apply(False)
    s = step(empty, settings)
    assert await s.group_usage("extra") == (0, 0)
    with psycopg.connect(mig(empty), autocommit=True) as conn:
        add_doc(conn, repo="kb-extra", audience="extra")
        add_doc(conn, repo="kb-extra", audience="extra")
        add_record(conn, audience="extra")
        add_record(conn, audience="all")
    assert await s.group_usage("extra") == (2, 1)


async def test_bi_roles_in_plan(empty, settings):
    await step(empty, settings).apply(False)
    mapped, stray = "kh_bi_aaaaaaaaaa", "kh_bi_bbbbbbbbbb"
    try:
        with psycopg.connect(empty.admin_dsn(), autocommit=True) as conn:
            for role in (mapped, stray):
                conn.execute(f"CREATE ROLE {role} NOLOGIN IN ROLE kh_bi")
        with psycopg.connect(mig(empty), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO kh.bi_roles (role_name, audience, created_by)"
                " VALUES (%s, 'all', 'a@example.test')",
                (mapped,),
            )
        s = step(empty, settings)
        planned = lines(await s.plan())
        assert f"i bi role {mapped} for all" in planned
        assert f"! bi role {stray} has no mapping" in planned
        # the "!" line does not block: apply still runs the database steps
        assert await run([s], "apply", False) == 2
        assert f"! bi role {stray} has no mapping" in lines(await s.apply(False))
        assert pending(mig(empty)) == []
    finally:
        with psycopg.connect(empty.admin_dsn(), autocommit=True) as conn:
            for role in (mapped, stray):
                conn.execute(f"DROP ROLE IF EXISTS {role}")
