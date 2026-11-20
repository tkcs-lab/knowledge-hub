"""The panel's op runner: one op at a time, ``kh`` subprocesses, a small job table (plan 63, 3.6).

A ``cli`` op runs ``python -m knowledge_hub.cli <argv>`` in its own session with an explicit
environment; its output is read as it arrives, capped, and scrubbed of the contents of every
``KH_*_FILE`` secret file. An ``inproc`` op calls the injected ``inproc`` coroutine instead.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import signal
import sys
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from knowledge_hub.panel import ops
from knowledge_hub.panel.context import PanelContext
from knowledge_hub.panel.ops import OpError, ValidatedOp

log = logging.getLogger(__name__)

MAX_JOBS = 50
MAX_OUTPUT = 8192
MAX_SECRET_FILE = 4096
MIN_SECRET = 8
READ_CHUNK = 4096
REDACTED = "***"
LOCK_REFUSAL = "admin: cannot lock"
# after a kill, how long to wait for the pipe to close and the process to be reaped
KILL_GRACE_SECONDS = 5

Inproc = Callable[[str, dict[str, Any], PanelContext], Awaitable[str]]


class ApiBusy(Exception):
    """Another op is running; the caller answers 409 ``busy``."""


def child_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """``PATH``, ``HOME``, ``PYTHONUNBUFFERED=1`` and every ``KH_*`` variable except ``KH_PANEL_*``."""
    source = os.environ if source is None else source
    env = {k: source[k] for k in ("PATH", "HOME") if k in source}
    env["PYTHONUNBUFFERED"] = "1"
    for key, value in source.items():
        if key.startswith("KH_") and not key.startswith("KH_PANEL_"):
            env[key] = value
    return env


def secret_values(*sources: Mapping[str, str]) -> list[str]:
    """The stripped contents of every ``KH_*_FILE`` file of at most 4096 bytes and at least 8 characters."""
    found: set[str] = set()
    for source in sources:
        for key, value in source.items():
            if not (key.startswith("KH_") and key.endswith("_FILE")) or not value:
                continue
            try:
                path = Path(value)
                if not path.is_file() or path.stat().st_size > MAX_SECRET_FILE:
                    continue
                text = path.read_bytes().decode("utf-8", errors="replace").strip()
            except OSError:
                continue
            if len(text) >= MIN_SECRET:
                found.add(text)
    # longest first, so a secret that contains another is replaced whole
    return sorted(found, key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


class _Capture:
    """Keeps the first characters of a byte stream and drops the rest.

    It keeps ``MAX_OUTPUT`` plus the longest secret, so a secret that crosses the cap is still
    found whole by ``redact`` before the text is cut to ``MAX_OUTPUT``.
    """

    def __init__(self, keep: int) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._parts: list[str] = []
        self._size = 0
        self._keep = keep

    def feed(self, data: bytes, final: bool = False) -> None:
        text = self._decoder.decode(data, final)
        room = self._keep - self._size
        if room <= 0 or not text:
            return
        text = text[:room]
        self._parts.append(text)
        self._size += len(text)

    def text(self, secrets: list[str]) -> str:
        return redact("".join(self._parts), secrets)[:MAX_OUTPUT]


class Runner:
    def __init__(
        self,
        ctx: PanelContext,
        *,
        inproc: Inproc | None = None,
        env: Mapping[str, str] | None = None,
        spawn: Callable[..., Awaitable[Any]] = asyncio.create_subprocess_exec,
        timeout: float = 300,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._ctx = ctx
        self._inproc = inproc
        self._env = dict(env) if env is not None else child_environment()
        self._spawn = spawn
        self._timeout = timeout
        self._now = now
        self._lock = asyncio.Lock()
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def config_file(self) -> str:
        return str(self._ctx.settings.config_file)

    async def submit(self, valid: ValidatedOp, email: str) -> str:
        if self._lock.locked():
            raise ApiBusy("another operation is running")
        await self._lock.acquire()
        try:
            job_id = uuid.uuid4().hex
            self._jobs[job_id] = {
                "id": job_id,
                "op": valid.name,
                "state": "running",
                "ok": None,
                "exit_code": None,
                "dry_run": valid.dry_run,
                "output": "",
                "restart_expected": False,
                "started_at": _iso(self._now()),
                "finished_at": None,
            }
            while len(self._jobs) > MAX_JOBS:
                self._jobs.popitem(last=False)
            task = asyncio.create_task(self._run(job_id, valid, email))
        except BaseException:
            self._lock.release()
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return dict(job) if job is not None else None

    async def wait_idle(self) -> None:
        """Waits for the running job, if any (tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ---- running ----

    async def _run(self, job_id: str, valid: ValidatedOp, email: str) -> None:
        ok, exit_code, output = False, None, ""
        try:
            secrets = secret_values(self._env, os.environ)
            if valid.kind == "inproc":
                ok, exit_code, output = await self._run_inproc(valid, secrets)
            else:
                exit_code, output = await self._run_cli(valid, secrets, email)
                ok = exit_code == 0 and LOCK_REFUSAL not in output
        except Exception:
            log.exception("panel op %s failed", valid.name)
            ok, output = False, "internal error"
        finally:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(
                    state="done",
                    ok=ok,
                    exit_code=exit_code,
                    output=output,
                    restart_expected=bool(ok and valid.op.is_config_op and not valid.dry_run),
                    finished_at=_iso(self._now()),
                )
            self._audit(job_id, valid, email, exit_code)
            self._lock.release()

    async def _run_inproc(self, valid: ValidatedOp, secrets: list[str]) -> tuple[bool, int, str]:
        if self._inproc is None:
            return False, 1, "this operation is not available"
        try:
            text = await self._inproc(valid.name, dict(valid.args), self._ctx)
        except OpError as exc:
            return False, 1, redact(exc.reason, secrets)[:MAX_OUTPUT]
        return True, 0, redact(str(text), secrets)[:MAX_OUTPUT]

    async def _run_cli(self, valid: ValidatedOp, secrets: list[str], email: str) -> tuple[int, str]:
        cmd = [sys.executable, "-m", "knowledge_hub.cli", *ops.argv(valid, self.config_file, email)]
        proc = await self._spawn(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=dict(self._env),
            start_new_session=True,
        )
        capture = _Capture(MAX_OUTPUT + (len(secrets[0]) if secrets else 0))
        reader = asyncio.create_task(self._read(proc, capture))
        timed_out = False
        try:
            exit_code = await asyncio.wait_for(self._finish(proc, reader), self._timeout)
        except TimeoutError:
            timed_out = True
            self._kill(proc)
            exit_code = -9
            await self._settle(proc, reader)
        except BaseException:
            # a failed read or a cancelled job must not leave the command running
            self._kill(proc)
            reader.cancel()
            raise
        output = capture.text(secrets)
        if timed_out:
            if output and not output.endswith("\n"):
                output += "\n"
            output += f"timed out after {self._timeout:g} s"
        return exit_code, output

    @staticmethod
    def _kill(proc: Any) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    async def _read(proc: Any, capture: _Capture) -> None:
        # read to the end even past the cap, so the child never blocks on a full pipe
        while True:
            data = await proc.stdout.read(READ_CHUNK)
            if not data:
                capture.feed(b"", final=True)
                return
            capture.feed(data)

    @staticmethod
    async def _finish(proc: Any, reader: asyncio.Task[None]) -> int:
        # shielded: a timeout cancels this wait, not the reader, which keeps draining
        await asyncio.shield(reader)
        return await proc.wait()

    @staticmethod
    async def _settle(proc: Any, reader: asyncio.Task[None]) -> None:
        await asyncio.wait({reader}, timeout=KILL_GRACE_SECONDS)
        if not reader.done():
            reader.cancel()
        elif not reader.cancelled() and reader.exception() is not None:
            log.warning("panel op output reader failed: %r", reader.exception())
        try:
            await asyncio.wait_for(proc.wait(), KILL_GRACE_SECONDS)
        except TimeoutError:
            log.warning("panel op process %s did not exit after SIGKILL", proc.pid)

    def _audit(self, job_id: str, valid: ValidatedOp, email: str, exit_code: int | None) -> None:
        line = {
            "ts": _iso(self._now()),
            "email": email,
            "op": valid.name,
            "args": valid.args,
            "dry_run": valid.dry_run,
            "exit_code": exit_code,
            "id": job_id,
        }
        print(json.dumps(line, sort_keys=True, default=str), flush=True)
