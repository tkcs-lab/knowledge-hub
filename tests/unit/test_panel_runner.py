import asyncio
import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge_hub.panel import ops, runner
from knowledge_hub.panel.ops import OpError, validate
from knowledge_hub.panel.runner import MAX_JOBS, MAX_OUTPUT, ApiBusy, Runner, child_environment

pytestmark = pytest.mark.unit

CONFIG = "/etc/kh/kh.yaml"


class FakeStream:
    """``stdout`` of a scripted process: the chunks, then EOF, or a hang until ``close``."""

    def __init__(self, chunks, hang=False):
        self.chunks = list(chunks)
        self.hang = hang
        self.closed = asyncio.Event()

    async def read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        if self.hang:
            await self.closed.wait()
        return b""


class FakeProc:
    def __init__(self, chunks=(), code=0, hang=False, pid=4242):
        self.pid = pid
        self.stdout = FakeStream(chunks, hang)
        self.code = code
        self.hang = hang
        self.killed = asyncio.Event()

    async def wait(self):
        if self.hang:
            await self.killed.wait()
            return -9
        return self.code

    def kill(self):
        self.killed.set()
        self.stdout.closed.set()


class FakeSpawn:
    def __init__(self, *procs):
        self.procs = list(procs)
        self.calls = []

    async def __call__(self, *cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        return self.procs.pop(0) if self.procs else FakeProc()


def make_ctx():
    return SimpleNamespace(settings=SimpleNamespace(config_file=Path(CONFIG)))


def label_add(dry_run=False):
    return validate("label.add", {"name": "ops", "description": "Operations"}, dry_run, None)


async def run_one(r, valid, email="admin@example.com"):
    job_id = await r.submit(valid, email)
    await r.wait_idle()
    return r.get(job_id)


@pytest.fixture
def killpg(monkeypatch):
    calls = []
    procs = {}

    def fake_killpg(pid, sig):
        calls.append((pid, sig))
        if pid in procs:
            procs[pid].kill()

    monkeypatch.setattr(runner.os, "killpg", fake_killpg)
    fake_killpg.calls = calls
    fake_killpg.procs = procs
    return fake_killpg


async def test_success_fills_the_job():
    spawn = FakeSpawn(FakeProc([b"diff\n", b"done\n"]))
    r = Runner(make_ctx(), env={}, spawn=spawn, now=lambda: 0.0)
    job = await run_one(r, label_add())
    assert job["state"] == "done"
    assert job["ok"] is True
    assert job["exit_code"] == 0
    assert job["output"] == "diff\ndone\n"
    assert job["op"] == "label.add"
    assert job["dry_run"] is False
    assert job["started_at"] == "1970-01-01T00:00:00+00:00"
    assert job["finished_at"] == "1970-01-01T00:00:00+00:00"
    assert set(job) == {
        "id", "op", "state", "ok", "exit_code", "dry_run", "output",
        "restart_expected", "started_at", "finished_at",
    }  # fmt: skip


async def test_submit_returns_at_once_with_a_running_job(killpg):
    proc = FakeProc(hang=True)
    killpg.procs[proc.pid] = proc
    r = Runner(make_ctx(), env={}, spawn=FakeSpawn(proc))
    job_id = await r.submit(label_add(), "a@example.com")
    assert len(job_id) == 32 and int(job_id, 16) >= 0
    await asyncio.sleep(0)
    job = r.get(job_id)
    assert job["state"] == "running"
    assert job["ok"] is None and job["exit_code"] is None
    proc.kill()
    await r.wait_idle()
    assert r.get(job_id)["state"] == "done"


async def test_argv_is_the_interpreter_and_ops_argv():
    spawn = FakeSpawn()
    r = Runner(make_ctx(), env={}, spawn=spawn)
    valid = label_add(dry_run=True)
    await run_one(r, valid)
    cmd, _ = spawn.calls[0]
    assert cmd == [sys.executable, "-m", "knowledge_hub.cli", *ops.argv(valid, CONFIG)]
    assert "--dry-run" in cmd and f"--config={CONFIG}" in cmd


async def test_spawn_options():
    spawn = FakeSpawn()
    r = Runner(make_ctx(), env={"KH_ENV": "test"}, spawn=spawn)
    await run_one(r, label_add())
    _, kwargs = spawn.calls[0]
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    assert kwargs["env"] == {"KH_ENV": "test"}


async def test_default_environment_has_kh_but_no_kh_panel(monkeypatch):
    monkeypatch.setattr(
        runner.os,
        "environ",
        {
            "PATH": "/usr/bin",
            "HOME": "/home/kh",
            "KH_ENV": "prod",
            "KH_CONFIG_FILE": CONFIG,
            "KH_DB_SETUP_PASSWORD_FILE": "/run/secrets/pg",
            "KH_PANEL_SESSION_KEY_FILE": "/run/secrets/key",
            "KH_PANEL_DEV_ADMIN": "a@example.com",
            "AWS_SECRET_ACCESS_KEY": "nope",
            "PYTHONPATH": "/evil",
        },
    )
    spawn = FakeSpawn()
    r = Runner(make_ctx(), spawn=spawn)
    await run_one(r, label_add())
    env = spawn.calls[0][1]["env"]
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/kh",
        "PYTHONUNBUFFERED": "1",
        "KH_ENV": "prod",
        "KH_CONFIG_FILE": CONFIG,
        "KH_DB_SETUP_PASSWORD_FILE": "/run/secrets/pg",
    }
    assert not [k for k in env if k.startswith("KH_PANEL_")]


def test_child_environment_without_path_or_home():
    assert child_environment({"KH_X": "1", "KH_PANEL_Y": "2"}) == {"PYTHONUNBUFFERED": "1", "KH_X": "1"}


async def test_output_is_capped_and_the_rest_drained():
    chunks = [b"x" * 4096 for _ in range(10)]
    proc = FakeProc(chunks)
    r = Runner(make_ctx(), env={}, spawn=FakeSpawn(proc))
    job = await run_one(r, label_add())
    assert job["output"] == "x" * MAX_OUTPUT
    assert proc.stdout.chunks == []  # everything was read
    assert job["ok"] is True


async def test_utf8_split_across_chunks_is_decoded():
    data = "é—ü".encode()
    proc = FakeProc([data[:1], data[1:4], data[4:]])
    r = Runner(make_ctx(), env={}, spawn=FakeSpawn(proc))
    job = await run_one(r, label_add())
    assert job["output"] == "é—ü"


def _secret_file(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


async def test_file_secrets_are_redacted(tmp_path):
    secret = "s3cret-Value-123"
    env = {
        "KH_DB_SETUP_PASSWORD_FILE": _secret_file(tmp_path, "pg", f"  {secret}\n".encode()),
        "KH_SHORT_FILE": _secret_file(tmp_path, "short", b"abc1234\n"),  # 7 characters: kept
        "KH_BIG_FILE": _secret_file(tmp_path, "big", b"B" * 4097),  # over 4096 bytes: kept
        "KH_MISSING_FILE": str(tmp_path / "missing"),
        "OTHER_FILE": _secret_file(tmp_path, "other", b"not-a-kh-secret"),
    }
    out = f"pw={secret} again {secret}; short abc1234; not-a-kh-secret; {'B' * 20}\n".encode()
    # split the secret across two chunks
    proc = FakeProc([out[:6], out[6:]])
    r = Runner(make_ctx(), env=env, spawn=FakeSpawn(proc))
    job = await run_one(r, label_add())
    assert secret not in job["output"]
    assert job["output"].startswith("pw=*** again ***;")
    assert "abc1234" in job["output"]
    assert "not-a-kh-secret" in job["output"]


async def test_secret_across_the_cap_is_redacted(tmp_path):
    secret = "ABCDEFGHIJKLMNOP"
    env = {"KH_TOKEN_FILE": _secret_file(tmp_path, "tok", secret.encode())}
    out = ("y" * (MAX_OUTPUT - 4) + secret + "tail").encode()
    r = Runner(make_ctx(), env=env, spawn=FakeSpawn(FakeProc([out])))
    job = await run_one(r, label_add())
    assert "ABCD" not in job["output"]
    assert job["output"] == "y" * (MAX_OUTPUT - 4) + "***" + "t"


async def test_timeout_kills_the_group(killpg):
    proc = FakeProc([b"working\n"], hang=True, pid=777)
    killpg.procs[proc.pid] = proc
    r = Runner(make_ctx(), env={}, spawn=FakeSpawn(proc), timeout=0.05)
    job = await run_one(r, label_add())
    assert killpg.calls == [(777, signal.SIGKILL)]
    assert job["exit_code"] == -9
    assert job["ok"] is False
    assert job["restart_expected"] is False
    assert job["output"] == "working\ntimed out after 0.05 s"


def test_timeout_message_of_the_default():
    r = Runner(make_ctx(), env={})
    assert r._timeout == 300
    assert f"timed out after {r._timeout:g} s" == "timed out after 300 s"


async def test_second_submit_while_running_is_busy(killpg):
    proc = FakeProc(hang=True)
    killpg.procs[proc.pid] = proc
    r = Runner(make_ctx(), env={}, spawn=FakeSpawn(proc, FakeProc()))
    await r.submit(label_add(), "a@example.com")
    with pytest.raises(ApiBusy):
        await r.submit(label_add(), "a@example.com")
    proc.kill()
    await r.wait_idle()
    job = await run_one(r, label_add())  # free again
    assert job["ok"] is True


async def test_table_keeps_the_last_50():
    r = Runner(make_ctx(), env={}, spawn=FakeSpawn())
    ids = []
    for _ in range(MAX_JOBS + 5):
        ids.append(await r.submit(label_add(), "a@example.com"))
        await r.wait_idle()
    assert MAX_JOBS == 50
    assert all(r.get(i) is None for i in ids[:5])
    assert all(r.get(i) is not None for i in ids[5:])
    assert len(r._jobs) == 50
    assert r.get("nope") is None


async def test_inproc_op_never_spawns():
    calls = []

    async def inproc(name, args, ctx):
        calls.append((name, args, ctx))
        return "queued rebuild of team-a"

    spawn = FakeSpawn()
    ctx = make_ctx()
    r = Runner(ctx, inproc=inproc, env={}, spawn=spawn)
    valid = validate("repo.rebuild", {"repo": "team-a"}, False, None)
    job = await run_one(r, valid)
    assert spawn.calls == []
    assert calls == [("repo.rebuild", {"repo": "team-a"}, ctx)]
    assert job["ok"] is True and job["exit_code"] == 0
    assert job["output"] == "queued rebuild of team-a"
    assert job["restart_expected"] is False


async def test_inproc_op_error_is_not_ok():
    async def inproc(name, args, ctx):
        raise OpError("repo team-z is not configured")

    spawn = FakeSpawn()
    r = Runner(make_ctx(), inproc=inproc, env={}, spawn=spawn)
    job = await run_one(r, validate("repo.rebuild", {"repo": "team-z"}, False, None))
    assert spawn.calls == []
    assert job["ok"] is False
    assert job["output"] == "repo team-z is not configured"


async def test_unexpected_error_frees_the_lock():
    async def inproc(name, args, ctx):
        raise RuntimeError("boom")

    r = Runner(make_ctx(), inproc=inproc, env={}, spawn=FakeSpawn())
    job = await run_one(r, validate("jobs.reclaim", {}, False, None))
    assert job["ok"] is False and job["output"] == "internal error"
    assert (await run_one(r, label_add()))["ok"] is True


@pytest.mark.parametrize(
    ("op", "args", "dry_run", "code", "output", "ok", "restart"),
    [
        ("label.add", {"name": "ops", "description": "D"}, False, 0, b"ok\n", True, True),
        ("label.add", {"name": "ops", "description": "D"}, True, 0, b"ok\n", True, False),
        ("label.add", {"name": "ops", "description": "D"}, False, 2, b"refused\n", False, False),
        ("label.add", {"name": "ops", "description": "D"}, False, 0, b"admin: cannot lock x\n", False, False),
        ("health.index_check", {}, False, 0, b"ok\n", True, False),
        ("health.setup_plan", {}, False, 0, b"ok\n", True, False),
    ],
)
async def test_restart_expected(op, args, dry_run, code, output, ok, restart):
    r = Runner(make_ctx(), env={}, spawn=FakeSpawn(FakeProc([output], code=code)))
    job = await run_one(r, validate(op, args, dry_run, None))
    assert job["ok"] is ok
    assert job["exit_code"] == code
    assert job["restart_expected"] is restart


async def test_audit_line(capsys):
    r = Runner(make_ctx(), env={}, spawn=FakeSpawn(FakeProc(code=2)), now=lambda: 60.0)
    job_id = await r.submit(label_add(dry_run=True), "admin@example.com")
    await r.wait_idle()
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines == [
        {
            "ts": "1970-01-01T00:01:00+00:00",
            "email": "admin@example.com",
            "op": "label.add",
            "args": {"name": "ops", "description": "Operations"},
            "dry_run": True,
            "exit_code": 2,
            "id": job_id,
        }
    ]


async def test_item_move_passes_the_session_email_and_expects_no_restart():
    spawn = FakeSpawn(FakeProc([b"{}\n"], code=0))
    r = Runner(make_ctx(), env={}, spawn=spawn)
    valid = validate(
        "item.move",
        {"id": "rec:0a1b2c3d-0000-4000-8000-0123456789ab", "from_group": "mgmt", "to_group": "all"},
        False,
        None,
    )
    job = await run_one(r, valid, email="a@b.test")
    cmd, _ = spawn.calls[0]
    assert "--by=a@b.test" in cmd
    assert job["ok"] is True
    assert job["restart_expected"] is False
